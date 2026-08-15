import os


def rewrite_proxy_for_runtime(url: str) -> str:
    """Make a host loopback proxy reachable from inside Docker."""
    in_docker = os.path.exists("/.dockerenv") or bool(os.getenv("DOCKER_CONTAINER"))
    if not in_docker:
        return url
    return (
        url.replace("://localhost:", "://host.docker.internal:")
        .replace("://127.0.0.1:", "://host.docker.internal:")
    )
