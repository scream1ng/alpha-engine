# Reports

This directory is the local research workspace for pipeline output.

Expected files after running the pipeline:

- `run_log.md`
- `agent_context.md`
- `agent_state.json`
- `<market>_regime_latest.md`
- `<market>_optimise_latest.md`
- `<market>_stability_latest.md`
- `<market>_report_latest.md`
- `<market>_chart_export_latest.md`
- `history/`

Policy:

- These files are generated and are not committed by default.
- Use them as the working review surface between runs.
- Compare `_latest.md` against `history/` locally.
- Commit `docs/chart_data.json` when you want GitHub Pages to publish a specific viewer snapshot.
