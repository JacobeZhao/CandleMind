# EMA Release V2 Contract Fixture

This directory holds a small, synthetic, immutable EMA V2 release used to test
release-verifier compatibility without reading external market data.

- `ema_release_v2_minimal.zip` contains only the frozen release files.
- `ema_release_v2_minimal.fixture.json` records the archive checksum, extracted
  file inventory and checksums, release digests, provenance, and tool versions.

The fixture was generated once with the legacy EMA release builder from commit
`192df97c0d1552f86f8db3733e45dc79eb24e9ac`, using Python 3.12.10,
pandas 2.2.3, NumPy 2.2.6, and PyArrow 23.0.0. Synthetic input references were
normalized to portable absolute paths:

```text
//fixture/input/BTCUSDT_5m.parquet
//fixture/input/universe.parquet
//fixture/input/pit_universe_manifest.json
//fixture/input/pit_readiness_audit.json
```

Tests must validate the archive against the sidecar and extract it safely. They
must not regenerate, rewrite, or update the fixture at test time. Any deliberate
replacement requires regenerating all checksums and provenance metadata in a
separate reviewed change.
