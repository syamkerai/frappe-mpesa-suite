from mpsa_suite.gateway.signing import sign, verify

BODY = '{"event":"payment.succeeded","intent":"MPI-26-10-000001"}'


def test_round_trip():
    header = sign("s3cret", BODY, timestamp=1_800_000_000)
    assert header.startswith("t=1800000000,v1=")
    assert verify("s3cret", BODY, header, now=1_800_000_010)
    assert verify("s3cret", BODY.encode(), header, now=1_800_000_010)


def test_rejects_tampering_wrong_secret_and_replay():
    header = sign("s3cret", BODY, timestamp=1_800_000_000)
    assert not verify("s3cret", BODY.replace("succeeded", "failed"), header, now=1_800_000_000)
    assert not verify("other", BODY, header, now=1_800_000_000)
    assert not verify("s3cret", BODY, header, now=1_800_000_000 + 301)  # stale
    assert not verify("s3cret", BODY, "t=abc,v1=00", now=1_800_000_000)
    assert not verify("s3cret", BODY, None)
    assert not verify("", BODY, header)
