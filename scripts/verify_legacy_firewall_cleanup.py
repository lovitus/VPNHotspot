#!/usr/bin/env python3
"""Exercise the IPv4 Clean commands from Rust source in a fresh network namespace.

Requires Linux, root (or passwordless sudo), util-linux and iptables-legacy.
--check-source only validates extraction; it does not count as a netfilter test.
"""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path("mobile/src/main/rust/vpnhotspotd/src/routing/firewall_cleanup.rs")
TABLES = ("mangle", "filter", "nat")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def extract(tree):
    text = (tree / SOURCE).read_text()
    match = re.search(
        r'pub\(super\) async fn clean\(\) \{(?P<deletes>.*?)'
        r'if let Err\(e\) = firewall::restore\(\s*IptablesTarget::Ipv4,\s*'
        r'"(?P<input>[^"\\]+)",\s*\)\s*\.await\s*\{\s*'
        r'report::io\("routing.clean_firewall.iptables_restore", e\);\s*\}',
        text, re.S,
    )
    require(match is not None, f"Unsupported IPv4 Clean structure: {tree / SOURCE}")
    prefix = re.sub(r"(?m)^\s*//[^\n]*", "", match["deletes"])
    call = re.compile(
        r'\s*delete_iptables_repeated\(\s*IptablesTarget::Ipv4,\s*'
        r'"(mangle|filter|nat)",\s*"(PREROUTING|INPUT|FORWARD|POSTROUTING)",\s*'
        r'&\["-j", "(vpnhotspot_[a-z_]+)"\],\s*\)\s*\.await;'
    )
    deletes, position = [], 0
    while found := call.match(prefix, position):
        deletes.append(found.groups())
        position = found.end()
    require(not prefix[position:].strip(), "Unparsed code before IPv4 restore; update the test parser")
    require(deletes, "No IPv4 jump deletions extracted")
    require(text.count("delete_iptables_repeated(") == len(deletes)
            and text[match.end():].lstrip().startswith("for rule in Runtime::ipv6_block_jump_rules()"),
            "Unexpected commands outside the extracted IPv4 cleanup")
    require(text.count("IptablesTarget::Ipv4") == len(deletes) + 1,
            "Unexpected additional IPv4 commands; update the test parser")
    payload = match["input"]
    require(payload.endswith("\n"), "Restore input must end with a newline")
    tables, active = [], False
    for line in payload.splitlines():
        if line.startswith("*"):
            require(not active and line[1:] in TABLES, f"Unexpected table: {line}")
            tables.append(line[1:])
            active = True
        elif line == "COMMIT":
            require(active, "COMMIT without a table")
            active = False
        else:
            require(active and re.fullmatch(r'(?::vpnhotspot_[a-z_]+ - \[0:0\]|-X vpnhotspot_[a-z_]+)', line),
                    f"Unsupported Clean command: {line}")
    require(not active and tuple(tables) == TABLES, "Unexpected IPv4 table sequence")
    return deletes, payload


def run(argv, *, input=None, check=True):
    result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=30)
    if check:
        require(result.returncode == 0, f"Command failed: {argv}\n{result.stdout}{result.stderr}")
    return result


def exercise(source, baseline):
    iptables = shutil.which("iptables-legacy")
    restore = shutil.which("iptables-legacy-restore")
    require(iptables and restore, "Install iptables-legacy and iptables-legacy-restore; no backend substitution")

    def rule(*args, table="filter"):
        return run([iptables, "-w", "-t", table, *args])

    def snapshot():
        return {table: rule("-S", table=table).stdout for table in TABLES}

    def fixture(kind):
        for table, chain in zip(TABLES, ("PREROUTING", "FORWARD", "POSTROUTING")):
            rule("-F", table=table)
            rule("-X", table=table)
            rule("-N", "regression_sentinel", table=table)
            rule("-A", chain, "-j", "regression_sentinel", table=table)
            rule("-A", "regression_sentinel", "-s", "192.0.2.1", "-j", "RETURN", table=table)
        rule("-P", "FORWARD", "DROP")
        expected = snapshot()
        if kind != "empty":
            rule("-N", "vpnhotspot_acl")
            rule("-A", "vpnhotspot_acl", "-i", "wlan0", "-s", "192.168.43.10", "-j", "ACCEPT")
        if kind in ("legacy", "mixed", "duplicate"):
            rule("-N", "vpnhotspot_fwd")
            for _ in range(3 if kind == "duplicate" else 1):
                rule("-A", "FORWARD", "-j", "vpnhotspot_fwd")
            rule("-A", "vpnhotspot_fwd", "-i", "wlan0", "-j", "vpnhotspot_acl")
            rule("-A", "vpnhotspot_fwd", "-o", "wlan0", "-m", "state", "--state",
                 "ESTABLISHED,RELATED", "-j", "vpnhotspot_acl")
            rule("-A", "vpnhotspot_fwd", "-i", "wlan0", "!", "-o", "wlan0", "-j", "REJECT")
        if kind in ("current", "mixed", "duplicate", "external"):
            for table, chain, target in (
                ("mangle", "PREROUTING", "vpnhotspot_dns_tproxy"),
                ("filter", "INPUT", "vpnhotspot_dns_input"),
                ("nat", "PREROUTING", "vpnhotspot_dns_nat"),
                ("nat", "POSTROUTING", "vpnhotspot_masquerade"),
            ):
                rule("-N", target, table=table)
                rule("-A", chain, "-j", target, table=table)
                rule("-A", target, "-j", "RETURN", table=table)
            rule("-N", "vpnhotspot_stats")
            rule("-A", "FORWARD", "-j", "vpnhotspot_acl")
            rule("-A", "vpnhotspot_acl", "-j", "vpnhotspot_stats")
            rule("-A", "vpnhotspot_stats", "-j", "ACCEPT")
        return expected

    def clean(commands):
        deletes, payload = commands
        for table, chain, target in deletes:
            text = f"*{table}\n-D {chain} -j {target}\nCOMMIT\n"
            while run([restore, "-w", "--noflush"], input=text, check=False).returncode == 0:
                pass
        return run([restore, "-w", "--noflush"], input=payload, check=False)

    if baseline:
        fixture("legacy")
        failed = clean(baseline)
        require(failed.returncode == 1 and "line 10 failed" in failed.stderr,
                f"Baseline did not reproduce line 10 failure: {failed.returncode}: {failed.stderr}")
        print("PASS baseline: original source reproduces line 10 failed")
    for kind in ("empty", "current", "legacy", "mixed", "duplicate"):
        expected = fixture(kind)
        result = clean(source)
        require(result.returncode == 0, f"{kind}: Clean failed: {result.stderr}")
        require(snapshot() == expected, f"{kind}: app state remains or unrelated rules changed")
        result = clean(source)
        require(result.returncode == 0 and snapshot() == expected, f"{kind}: repeated Clean is not idempotent")
        print(f"PASS {kind}: cleanup, sentinel preservation and idempotence")
    fixture("external")
    rule("-N", "external_owner")
    rule("-A", "FORWARD", "-j", "external_owner")
    rule("-A", "external_owner", "-j", "vpnhotspot_acl")
    foreign = rule("-S", "external_owner").stdout
    result = clean(source)
    require(result.returncode != 0, "Clean silently removed an external ACL reference")
    require(rule("-S", "external_owner").stdout == foreign, "External owner rules changed")
    rule("-C", "FORWARD", "-j", "external_owner")
    print("PASS external: failure remains visible and foreign references remain intact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--check-source", action="store_true")
    parser.add_argument("--parent-netns", help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = extract(args.source.resolve())
    baseline = extract(args.baseline_source.resolve()) if args.baseline_source else None
    if args.check_source:
        print(f"Source extraction OK: {len(source[0])} jump deletions, {len(source[1].splitlines())} restore lines.")
        print("Static check only; netfilter behavior has NOT been tested.")
        return
    current = os.readlink("/proc/self/ns/net")
    if not args.parent_netns:
        command = (["sudo", "-n"] if os.geteuid() else []) + [
            "unshare", "--net", "--", sys.executable, str(Path(__file__).resolve()),
            "--source", str(args.source.resolve()), "--parent-netns", current,
        ]
        if args.baseline_source:
            command += ["--baseline-source", str(args.baseline_source.resolve())]
        raise SystemExit(subprocess.run(command, check=False).returncode)
    require(current != args.parent_netns and current != os.readlink("/proc/1/ns/net"),
            "Refusing to touch the parent or init network namespace")
    require(os.geteuid() == 0, "Network namespace tests require root")
    exercise(source, baseline)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
