//! Isolated-network regression driver using the production rule selector.
use futures_util::TryStreamExt;
use rtnetlink::IpVersion;
use vpnhotspotd::shared::legacy_routing::legacy_ipv4_rules_to_delete;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (connection, handle, _) = rtnetlink::new_connection()?;
    let task = tokio::spawn(connection);
    let rules = handle
        .rule()
        .get(IpVersion::V4)
        .execute()
        .try_collect::<Vec<_>>()
        .await?;
    for rule in legacy_ipv4_rules_to_delete(&rules) {
        handle.rule().del(rule).execute().await?;
    }
    task.abort();
    Ok(())
}
