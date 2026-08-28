# QDC507 module voice artifacts

These files are a read-only copy of the existing `mavo/Resources/ModuleVoice`
artifacts. The gateway does not load them at startup. Set
`QDC507_MODULE_VOICE_MANIFEST` and `QDC507_MODULE_VOICE_RESOURCE_DIR` only
after verifying the module kernel release, ALSA device layout, and artifact
hashes against the target device.

The packaged manifest names the bundled `qdc507_voice.ko`. The accompanying
historical `MODULE-REPORT.md` also discusses an alternate `qdc507_afe.ko`
revision that is not present in this directory; that section must not be used
as proof that the bundled voice module is safe on a different firmware build.

The runtime checks the manifest, module root, `uname -r`, `/dev/snd`,
`/dev/ttyGS0`, and `/run/voc_svr` before loading anything. It never forces a
module unload.
