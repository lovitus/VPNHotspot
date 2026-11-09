//! Identify the pre-v3 IPv4 policy-rule group without deleting by priority alone.

use std::collections::HashSet;

use rtnetlink::packet_route::{
    rule::{RuleAction, RuleAttribute, RuleMessage},
    AddressFamily,
};

/// Returns the downstream only for the exact unmarked, all-address legacy shape.
/// Lookup rules must additionally be paired with a legacy guard on that interface.
fn legacy_ipv4_rule_interface(rule: &RuleMessage) -> Option<&str> {
    if rule.header.family != AddressFamily::Inet
        || rule.header.src_len != 0
        || rule.header.dst_len != 0
        || rule.header.tos != 0
        || !rule.header.flags.is_empty()
    {
        return None;
    }
    let mut interface = None;
    let mut priority = None;
    let mut table = None;
    for attribute in &rule.attributes {
        match attribute {
            RuleAttribute::Iifname(value) if interface.is_none() => {
                interface = Some(value.as_str())
            }
            RuleAttribute::Priority(value) if priority.is_none() => priority = Some(*value),
            RuleAttribute::Table(value) if table.is_none() => table = Some(*value),
            _ => return None,
        }
    }
    let interface = interface.filter(|name| !name.is_empty() && *name != "lo")?;
    let table = table.unwrap_or(u32::from(rule.header.table));
    match (priority?, rule.header.action) {
        (17800 | 17900, RuleAction::ToTable) if table >= 1000 => Some(interface),
        (17980, RuleAction::Unreachable) if table == 0 => Some(interface),
        _ => None,
    }
}

/// Snapshot the exact rules owned by a recognizable old downstream group.
/// Guards are returned last so failure cannot silently enable system fallback.
pub fn legacy_ipv4_rules_to_delete(rules: &[RuleMessage]) -> Vec<RuleMessage> {
    let downstreams = rules
        .iter()
        .filter(|rule| rule.header.action == RuleAction::Unreachable)
        .filter_map(legacy_ipv4_rule_interface)
        .collect::<HashSet<_>>();
    [false, true]
        .into_iter()
        .flat_map(|guard| {
            rules
                .iter()
                .filter(move |rule| (rule.header.action == RuleAction::Unreachable) == guard)
        })
        .filter(|rule| {
            legacy_ipv4_rule_interface(rule).is_some_and(|name| downstreams.contains(name))
        })
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(priority: u32, table: u32, action: RuleAction) -> RuleMessage {
        let mut rule = RuleMessage::default();
        rule.header.family = AddressFamily::Inet;
        rule.header.action = action;
        rule.attributes = vec![
            RuleAttribute::Priority(priority),
            RuleAttribute::Iifname("p2p0".to_owned()),
            RuleAttribute::Table(table),
        ];
        rule
    }

    #[test]
    fn recognizes_observed_legacy_group() {
        for rule in [
            rule(17800, 1031, RuleAction::ToTable),
            rule(17900, 1016, RuleAction::ToTable),
            rule(17980, 0, RuleAction::Unreachable),
        ] {
            assert_eq!(legacy_ipv4_rule_interface(&rule), Some("p2p0"));
        }
    }

    #[test]
    fn preserves_current_rules_and_system_tables() {
        for rule in [
            rule(20700, 1042, RuleAction::ToTable),
            rule(20800, 1024, RuleAction::ToTable),
            rule(20900, 0, RuleAction::Unreachable),
            rule(17800, 97, RuleAction::ToTable),
            rule(17900, 99, RuleAction::ToTable),
            rule(17980, 1042, RuleAction::ToTable),
        ] {
            assert_eq!(legacy_ipv4_rule_interface(&rule), None);
        }
    }

    #[test]
    fn preserves_selective_or_marked_custom_rules() {
        for attribute in [
            RuleAttribute::FwMark(1),
            RuleAttribute::FwMask(1),
            RuleAttribute::Oifname("tun0".to_owned()),
            RuleAttribute::Priority(17800),
        ] {
            let mut message = rule(17800, 1031, RuleAction::ToTable);
            message.attributes.push(attribute);
            assert_eq!(legacy_ipv4_rule_interface(&message), None);
        }
        for field in 0..4 {
            let mut message = rule(17800, 1031, RuleAction::ToTable);
            match field {
                0 => message.header.src_len = 24,
                1 => message.header.dst_len = 24,
                2 => message.header.tos = 1,
                _ => message.header.family = AddressFamily::Inet6,
            }
            assert_eq!(legacy_ipv4_rule_interface(&message), None);
        }
    }

    #[test]
    fn requires_matching_guard_and_preserves_duplicate_order() {
        let lookup = rule(17800, 1031, RuleAction::ToTable);
        let guard = rule(17980, 0, RuleAction::Unreachable);
        assert!(legacy_ipv4_rules_to_delete(std::slice::from_ref(&lookup)).is_empty());
        let mut other = lookup.clone();
        other
            .attributes
            .retain(|value| !matches!(value, RuleAttribute::Iifname(_)));
        other
            .attributes
            .push(RuleAttribute::Iifname("wlan1".to_owned()));
        let rules = vec![guard.clone(), lookup.clone(), other, lookup.clone()];
        assert_eq!(
            legacy_ipv4_rules_to_delete(&rules),
            vec![lookup.clone(), lookup, guard]
        );
    }

    #[test]
    fn requires_downstream_interface() {
        let mut message = rule(17980, 0, RuleAction::Unreachable);
        message
            .attributes
            .retain(|value| !matches!(value, RuleAttribute::Iifname(_)));
        assert_eq!(legacy_ipv4_rule_interface(&message), None);
        message
            .attributes
            .push(RuleAttribute::Iifname("lo".to_owned()));
        assert_eq!(legacy_ipv4_rule_interface(&message), None);
    }
}
