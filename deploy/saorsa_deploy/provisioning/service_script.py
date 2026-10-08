"""Deploy a client VM's host-side Python service script.

One definition, shared by the provisioners that install these scripts and by
`deployments upgrade-wasm-client`, which has to redeploy the same file when it
replaces a client bundle (V2-1460). Keeping the heredoc in one place is the
point: a second copy is how a VM ends up running a script from one release with
a client from another.
"""

from saorsa_deploy.resources import get_resources_dir


def build_service_script_commands(scripts_dir: str, script_name: str) -> list[str]:
    """Write ``script_name`` from this package's resources into ``scripts_dir``.

    A quoted heredoc delimiter, so nothing in the script body is expanded by
    the shell on the way in.
    """
    script = (get_resources_dir() / "scripts" / script_name).read_text()
    return [
        f"cat > {scripts_dir}/{script_name} << 'SCRIPT_EOF'\n{script}SCRIPT_EOF",
        f"chmod +x {scripts_dir}/{script_name}",
    ]
