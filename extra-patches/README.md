Drop patches for optional features here. A feature that is switched on while its file is
missing aborts the build (no silent skips).

  lz4kd.patch    / lz4kd.config      zram LZ4KD       (workflow input LZ4KD)
  rekernel.patch / rekernel.config   Re:Kernel        (workflow input REKERNEL)

.config files are plain `CONFIG_X=y` lines appended to gki_defconfig.
