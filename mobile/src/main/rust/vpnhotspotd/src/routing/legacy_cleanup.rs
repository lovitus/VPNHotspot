use std::io;

use rtnetlink::IpVersion;
use vpnhotspotd::shared::legacy_routing::legacy_ipv4_rules_to_delete;
use vpnhotspotd::shared::protocol::IoResultReportExt;

use crate::netlink;

use super::netlink_commands::is_missing;

pub(super) async fn clean(connection: &mut netlink::RequestConnection) -> io::Result<()> {
    let rules = connection
        .dump_rules(IpVersion::V4)
        .await
        .with_report_context("routing.clean_legacy_rules.dump")?;
    // Preserve every selector in the kernel snapshot, including detached interfaces.
    for rule in legacy_ipv4_rules_to_delete(&rules) {
        match connection.delete_rule(rule.clone()).await {
            Ok(()) => {}
            Err(error) if is_missing(&error) => {}
            Err(error) => {
                return Err(error).with_report_context_details(
                    "routing.clean_legacy_rules.delete",
                    [("rule", format!("{rule:?}"))],
                );
            }
        }
    }
    Ok(())
}
