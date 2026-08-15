from backend.app import proxy


def test_proxy_is_unchanged_outside_docker(monkeypatch):
    monkeypatch.setattr(proxy.os.path, "exists", lambda _path: False)
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)

    assert (
        proxy.rewrite_proxy_for_runtime("http://127.0.0.1:7897")
        == "http://127.0.0.1:7897"
    )


def test_loopback_proxy_is_rewritten_inside_docker(monkeypatch):
    monkeypatch.setattr(proxy.os.path, "exists", lambda path: path == "/.dockerenv")

    assert (
        proxy.rewrite_proxy_for_runtime("http://127.0.0.1:7897")
        == "http://host.docker.internal:7897"
    )
    assert (
        proxy.rewrite_proxy_for_runtime("socks5://localhost:7898")
        == "socks5://host.docker.internal:7898"
    )
