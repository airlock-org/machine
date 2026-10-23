//! Compile this machine's Rust declarations into the artifacts reviewed and deployed by Airlock.

use airlock_config::{components, model};

mod machine;

/// Prepare this checkout using the shared compiler and validation profiles.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    airlock_config::preparation::run_machine(
        &machine::machine()?,
        include_str!("../application-revision").trim(),
    )
}
