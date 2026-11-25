# WowSpace

A sound enhancer for Arch Linux (any PipeWire desktop) modelled on the old
SRS WOW Thing Winamp plug-in: 3D Space, 3D Center, TruBass (bass resonance
plus generated bass harmonics), Focus and a headphone crossfeed.

The app adds a **WowSpace** output device using PipeWire's built-in
filter-chain, so there is nothing to compile and every slider changes the
sound live. It sits in the system tray (KDE, GNOME with the AppIndicator
extension, and others).

## Install (Arch Linux)

```bash
git clone <this repo> wowspace
cd wowspace
makepkg -si
```

Then start **WowSpace** from your application menu. To uninstall:

```bash
sudo pacman -R wowspace
```

Dependencies: `python`, `pyside6`, `pipewire`, `wireplumber`.

Only stereo is processed: WowSpace is a 2-channel output device.

## Use

- **On / Off** (button or tray menu): On makes WowSpace your default output and
  sends the processed sound to your real device. Off switches your default back.
- **Play through**: which device WowSpace plays into. "Your previous default
  output" uses whatever was default when you turned it on.
- **Presets**: Classic, Headphones, Max WOW, Bass Head, Subtle, plus your own
  (Save…). Also in the tray menu.
- Closing the window keeps WowSpace running in the system tray. Use **Quit** in
  the tray menu to stop it; that also switches your output back.
- **Start WowSpace when I log in** adds it to your session's autostart, in the tray.

Settings live in `~/.config/wowspace/settings.json`. If the device fails to
start, the log is at `~/.local/state/wowspace/pipewire.log`.

`wowspace --print-config` prints the PipeWire filter-chain for your current
settings, if you'd rather install it as a permanent config in
`~/.config/pipewire/pipewire.conf.d/` and skip the app.

## Web builder

[`web/wowspace-builder.html`](web/wowspace-builder.html) is a single-page
version you can open in a browser: preview the effect on a demo loop or your
own track, then export a zip with

- a 4-channel true-stereo impulse plus a PipeWire filter-chain config and
  `install.sh`,
- an EasyEffects output preset (Bass Enhancer → Convolver → Limiter) and
  `install-easyeffects.sh`,
- a 14-channel HeSuVi-format impulse for HeSuVi / Equalizer APO on Windows.

The impulse files hold only the linear part of the effect; the generated
TruBass harmonics need the app or the EasyEffects preset.

## How it works

The input is split into mid (L+R) and side (L−R). The side signal gets an
SRS-style perspective EQ (lift around 350 Hz and above 7 kHz, dip around
3 kHz) and is widened; the mid signal gets the TruBass resonance at the
chosen speaker size. TruBass harmonics come from full-wave rectifying the
low-passed bass (|x| = 2·max(x, 0) − x, using PipeWire's clamp, invert and
mixer blocks) and keeping the harmonics above the speaker size, so they
follow the music's level. Focus adds presence near 3 kHz and air above
9 kHz. Headphone mode crossfeeds a low-passed, 0.28 ms delayed copy of each
side into the other ear. A hard clip at 0 dBFS guards the output; leave Auto
headroom on to stay clear of it.

This is an independent recreation of the sound, based on public descriptions
of the SRS techniques. It is not affiliated with SRS Labs or its successors.

## License

MIT
