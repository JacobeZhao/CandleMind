# Documentation Index

Use this index to distinguish current operating rules from historical research.

## Canonical Guidance

- [`DATA_LAYOUT.md`](DATA_LAYOUT.md): authoritative repository/G-drive
  ownership boundary, directory layout, and model release rules.
- [`../backend/scripts/README.md`](../backend/scripts/README.md): supported
  maintenance, training, evaluation, and artifact commands.
- [`../AGENTS.md`](../AGENTS.md): contributor workflow, style, testing, and
  security requirements.

`G_DRIVE_README.md` mirrors operational context intended for the external data
store. When its content conflicts with `DATA_LAYOUT.md`, follow
`DATA_LAYOUT.md` and update the external copy deliberately.

## Research History

Research documents record time-specific experiments and decisions; they are
not deployment instructions. RL reports are archived under
[`research/rl/`](research/rl/). Read that directory's index before using a
command or metric from an older report.

Generated reports belong under
`G:\CandleMind\CandleMind_data\experiments\reports`, not in this directory.
Only durable decisions and reproducibility notes should be committed here.
