# Toolchain versions and commits (captured 2026-11-21)

## Open-source FPGA toolchain (openXC7)

Installed via `openXC7/toolchain-installer`
(https://github.com/openXC7/toolchain-installer, commit
`9362f280d6d2397ef39657b98616cc6ca3fe3ff6`, 2026-09-02).

| Component   | Commit                                    | Repo |
|-------------|--------------------------------------------|------|
| yosys       | `38e001a6ff74ca434bf4cc02c053f53619160ab0` (v0.68) | https://github.com/YosysHQ/yosys.git |
| nextpnr-xilinx | `bd9c74c5b90b65e33a5bcd1de124982441375768` (v0.9.3-26-gbd9c74c5) | https://github.com/openXC7/nextpnr-xilinx.git |
| prjxray (python/utils) | `e1302fc6f8662a237e42790073fa641f2d9f1371` | https://github.com/openXC7/prjxray.git |
| prjxray-db  | `7a36171801cb7f4cae920aa0eda675a986649f6e` | https://github.com/openXC7/prjxray-db.git |

Installed to `/opt/openxc7` (`bin/`, `lib/`, `share/`, `venv/`).

**No patches were made to `toolchain-installer`'s own tracked files.**
The installer's known issues (see HANDOFF.md) were worked around *outside*
the installer rather than by patching it:
- yosys: installer assumes a classic Makefile build; newer yosys dropped
  that for CMake. Built directly with `cmake -B build
  -DCMAKE_INSTALL_PREFIX=/opt/openxc7 . && cmake --build build -j$(nproc)
  && cmake --install build` instead of letting the installer script
  handle it.
- prjxray's Python side (needed for `fasm2frames`) never gets installed
  by the script at all. Fixed with a dedicated venv
  (`/opt/openxc7/venv`) plus the wrapper script in this directory
  (`fasm2frames`), which should be placed at `/opt/openxc7/bin/fasm2frames`.

`export.sh` in this directory is the exact file from `/opt/openxc7/export.sh`
-- source it to get `yosys`/`nextpnr-xilinx`/etc on `PATH`.

## Flashing

- **Vivado Lab Edition v2026.1** (64-bit), SW Build 6511674, 2026-06-16.
  Tool version limit 2026.06. On Arch Linux this came from the
  `vivado-lab-edition` AUR package; the raw installer is AMD/Xilinx's
  `xsetup --batch Install`. **No macOS build exists** -- see the main
  repo's `MACOS-SETUP.md`.
  - Needed `libtinfo.so.5`/`libncurses.so.5` copied from the bundled
    `lib/lnx64.o/SuSE/` directory to run on a modern distro.
  - JTAG programming requires `connect_hw_server -allow_non_jtag` in the
    Tcl script (not needed for a genuine Xilinx cable, only for this
    cable clone) -- see `fpga/*/flash.tcl` patterns in the main repo.
- **openFPGALoader v1.1.1** (Arch package `openfpgaloader 1.1.1-2`) --
  installed, but **does not work with this specific JTAG cable**. Kept
  around in case a different/genuine cable is used later.

## LiteX and its component repos

venv at `~/Projects/litex/litex-venv`. Full `pip freeze` output is in
`pip-freeze.txt` in this directory. The key pinned commits (also in that
file as `git+` URLs, extracted here for convenience):

| Repo | Commit |
|------|--------|
| litex | `cfb382f54f81a1b3e6d7fcaa9db1a011007dd764` |
| litex-boards | `b8424ee7487281c29750c75ffee4532d56db5015` |
| migen | `4c2ae8dfeea37f235b52acb8166f12acaaae4f7c` |
| litedram | `3cf585a60a37113f18a9f6c6f3ee774be521623e` |
| liteeth | `8c9150ff121cb3148d8ea26ce3b1c5200479848d` |
| litescope | `6bf3b92f261c50b8c7c74947f84e692ae846f512` |
| litespi | `2e211447af44d9f015fd1c50c5b10d399b640972` |
| litei2c | `81cf75d3e6fc8ddfe4dece68cd7e2b39a2a4385e` |
| liteiclink | `8a4ce305510614266dad462dbe6b1f154f7487f4` |
| litejesd204b | `84734f7f63ecd5e27b0cdd0d175cfcfe521c2bfa` |
| litepcie | `0439d6534a9e41f9605c6dbb52d926614b2fbeec` |
| litesata | `d3d2b92f634730b05005cc565a10b43289b411e7` |
| litesdcard | `227d61bc2b92ca56cac78a539b98e378468b1ba1` |
| litedsp | `4ff1ea2bafd12452e59bf456bc4668d07bbb6b01` |
| pythondata-cpu-vexriscv | `642ecfed1c84460555d6d803d660cc60cfc1ecb6` |
| pythondata-cpu-vexriscv-smp | `217d23d7e9ad5556c17a73dc6ffc1971765f3d7c` |
| pythondata-cpu-vexiiriscv | `15cfab529a17c473d0fc75edf3f409eb374cef35` |
| pythondata-cpu-naxriscv | `20da269306bb3bfabd09de08d4c1be1fbc202474` |
| pythondata-cpu-serv | `111947d7ab652c28642d7ff0a528dae293ca4601` |
| pythondata-cpu-minerva | `ab328774891c70694c6576be19d1d3e427d9f435` |
| pythondata-cpu-mor1kx | `ba6ea16dc1250ac138ea0d923af4f91da790892f` |
| pythondata-cpu-lm32 | `0f1d1b91202b95a9b749a745848430d64afb4400` |
| pythondata-cpu-sentinel | `7ec5e1e5db1a53910e4c58ae4c098ebce3f9591f` |
| pythondata-cpu-veer_eh1 | `8d01ede7ea2ff40d4526616b0e2c91de0142508e` |
| pythondata-misc-tapcfg | `a12c3f592c99f9c082fdc68c065b81cbd6e6b238` |
| pythondata-misc-usb_ohci | `17c1d3d6548ea267e19aec3cb6d2e64335a1bb2a` |
| pythondata-software-compiler_rt | `6eb76609c9627bf26635e57c63fb22cda7115887` |
| pythondata-software-picolibc | `6a13ccce7c575b32c102dd9dc52178505b81fe39` |

The `litex_boards` files for this board (`litex_boards/targets/
qmtech_kintex7_devboard.py` and `litex_boards/platforms/
qmtech_kintex7_devboard.py`) were edited locally in the `litex-boards`
checkout -- those edits are already mirrored into the main repo's
`litex-soc/qmtech_kintex7_devboard.py` and
`litex-soc/qmtech_kintex7_devboard_platform.py` (confirmed byte-identical
as of this capture), so they're committed, not lost work.

## RISC-V GCC

```
riscv64-linux-gnu-gcc (GCC) 16.2.1 20260810
```
Installed via the OS package manager (not LiteX's own
`litex_setup.py --gcc=riscv`, which tried a root `pacman` call and
failed without privilege -- installed manually instead). This is a
Linux-targeted cross-gcc (`riscv64-linux-gnu-*`) used purely as a
bare-metal RV32IM cross-compiler here (`-march=rv32i2p0_m -mabi=ilp32`);
on macOS the equivalent is an `elf`-targeted toolchain (e.g. xPack's
`riscv-none-elf-gcc`), not this exact package.
