fn main() -> Result<(), Box<dyn std::error::Error>> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    // SAFETY: build scripts run single-threaded before code generation.
    unsafe { std::env::set_var("PROTOC", protoc) };
    let well_known = protoc_bin_vendored::include_path()?;
    // The same protos the Python host and the web client generate theirs from.
    let api_root = "../..";
    let api = "../../omnigent/api/workspace/v1alpha1/workspace.proto";
    let helper = "../../omnigent/api/workspace/v1alpha1/workspace_helper.proto";
    tonic_prost_build::configure()
        .compile_protos(&[api, helper], &[api_root, well_known.to_str().unwrap()])?;
    println!("cargo:rerun-if-changed={api}");
    println!("cargo:rerun-if-changed={helper}");
    // The release build names the build; see build_info.rs.
    println!("cargo:rerun-if-env-changed=WORKSPACE_HELPER_BUILD_VERSION");
    println!("cargo:rerun-if-env-changed=WORKSPACE_HELPER_BUILD_COMMIT");
    Ok(())
}
