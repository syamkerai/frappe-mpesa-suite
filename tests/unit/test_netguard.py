import pytest

from mpsa_suite.gateway.netguard import is_public_url


def fake_resolver(mapping):
    def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return [(None, None, None, None, (ip, 0)) for ip in mapping[host]]
    return resolve


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x", "https://10.1.2.3/x", "https://192.168.0.5/", "https://169.254.169.254/latest/meta-data",
    "https://[::1]/", "https://0.0.0.0/", "https://[::ffff:10.0.0.1]/", "not a url", "",
])
def test_blocks_private_and_invalid(url):
    assert not is_public_url(url, resolve=fake_resolver({}))


def test_dns_names():
    r = fake_resolver({"hooks.example.com": ["93.184.216.34"], "evil.example.com": ["93.184.216.34", "10.0.0.7"],
                       "internal.example.com": ["172.16.4.4"]})
    assert is_public_url("https://hooks.example.com/mpesa", resolve=r)
    assert not is_public_url("https://evil.example.com/", resolve=r)  # any private answer blocks
    assert not is_public_url("https://internal.example.com/", resolve=r)
    assert not is_public_url("https://unknown.example.com/", resolve=r)
    assert is_public_url("https://93.184.216.34/hook", resolve=r)
