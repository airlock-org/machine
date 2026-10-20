//! Compile this machine's Rust declarations into the artifacts reviewed and deployed by Airlock.

use airlock_config::{components, model};
use std::path::PathBuf;

mod physical;

mod bootstrap;

/// Emit the physical machine and nested validation environments without building or deploying them.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let emit = |directory: &std::path::Path| {
        airlock_config::export_machine(
            &physical::machine()?,
            &bootstrap::machine()?,
            include_str!("../application-revision").trim(),
            directory,
        )
    };

    match arguments.as_slice() {
        [command, directory] if command == "--emit" => emit(&PathBuf::from(directory)),
        [command, cache] if command == "prepare" => {
            let tree = airlock_config::preparation::prepare(
                &std::env::current_dir()?,
                &PathBuf::from(cache),
                emit,
            )?;
            println!("path:{path}", path = tree.display());
            Ok(())
        }
        _ => Err("usage: airlock-machine-config prepare CACHE | --emit DIRECTORY".into()),
    }
}
