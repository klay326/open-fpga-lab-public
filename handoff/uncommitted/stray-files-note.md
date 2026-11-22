# Stray files found outside the git repo (not lost work, just cruft)

- `~/Projects/litex/software-demos/led-chase/` has several leftover
  `vivado_lab*.jou` / `vivado_lab*.log` journal/log files from running
  `vivado_lab` with the wrong working directory at various points this
  session. Harmless, safe to delete; not copied into this handoff bundle.
- `~/Projects/litex/build-qmtech-k7/` (no suffix) is a stale, superseded
  build directory from early in the session (before settling on the
  `-minimal` / `-cm4` naming), with `build.log`/`build2.log`/`build3.log`
  left in it. Not used for anything; superseded by
  `build-qmtech-k7-minimal` and `build-qmtech-k7-cm4`, which this bundle
  does carry forward.
- `~/Projects/demo-projects/` is a full clone of `openXC7/demo-projects`
  (many boards, not just this one) -- only `blinky-qmtech/` and
  `button-chase-qmtech/` are relevant here, and their source files are
  already committed in the main repo's `fpga/blinky/` and
  `fpga/button-chase/`. Their *built* `.bit` files (gitignored) were
  pulled into `bitstreams/` in this bundle.
- `~/Projects/litex/litex-boards/litex_boards/{targets,platforms}/
  qmtech_kintex7_devboard.py` show as locally modified in that checkout's
  `git status`, but diffed byte-identical against the main repo's
  `litex-soc/qmtech_kintex7_devboard.py` and
  `litex-soc/qmtech_kintex7_devboard_platform.py` as of this capture --
  i.e. already committed, nothing uncommitted there.

**The main project repo itself (`~/Projects/qmtech-k7-riscv-computer`) had
a clean working tree with nothing uncommitted** at the time this bundle
was built -- see `project-repo-git-status.txt`.
