from pathlib import Path


def test_ui_nginx_api_proxy_preserves_backend_route_paths() -> None:
    config = Path("ui/nginx.conf").read_text(encoding="utf-8")

    assert "rewrite ^/api/(.*)$ /$1 break;" in config
    assert "proxy_pass $pheasant_upstream;" in config
    assert "proxy_pass $pheasant_upstream/;" not in config
