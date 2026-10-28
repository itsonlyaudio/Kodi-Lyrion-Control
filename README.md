# Kodi-Lyrion-Control

This Kodi audio add-on provides a remote-friendly screen for controlling a
player on a Lyrion Music Server (formerly Logitech Media Server). It displays
the current track and artwork, the server playlist, and the current track
position.

## Install

Copy this directory to Kodi's add-ons directory as `plugin.audio.lyrion`, then
enable **Lyrion** in Kodi. For installation from ZIP, the archive must contain
the add-on folder at its root, with `addon.xml` inside that folder:

```text
plugin.audio.lyrion-2.1.5-kodi21.zip
└── plugin.audio.lyrion/
    ├── addon.xml
    ├── default.py
    ├── settings.xml
    └── resources/
```

Do not zip the add-on's contents directly; Kodi expects
`plugin.audio.lyrion/addon.xml`, not `addon.xml` at the ZIP root. Exclude
macOS `__MACOSX`/`._*` metadata and Python `__pycache__` files.

## Configure

By default, the add-on searches the local network for Lyrion servers using its
UDP discovery protocol (port `3483`) and uses the discovered server's JSON-RPC
port. Kodi and the server need to be on the same local network; network
segmentation or firewall rules can prevent broadcast discovery.

If more than one server is found, choose one from the selection dialog. If
discovery finds none, the add-on falls back to the configured host. Open the
add-on settings to enter or change:

- **Discover server automatically**: enabled by default.
- **Server host**: DNS name or IP address, used when discovery is disabled or
  as a fallback when discovery finds no server.
- **Server port**: usually `9000`, used with the manual/fallback host.
- **Player ID**: optional. If left blank, the add-on queries the server's
  connected players and selects the only player automatically or prompts you
  to choose one. The selected player ID is saved in the setting.
- **Username / Password**: optional HTTP authentication credentials.

The server's JSON-RPC endpoint must be reachable from Kodi. The add-on uses
`/jsonrpc.js` over HTTP and automatically refreshes playback once per second
while its screen is open. The title shows the server's configured music library
name and selected player name when available. Refresh dispatch has a single
Kodi-wide owner so stale add-on instances cannot issue competing status requests.
Playback and volume commands are sent without waiting for an extra status
request; the regular refresh updates the display afterward.
Kodi GUI sounds are muted while the add-on screen is active and the previous
GUI-sound mode is restored when Kodi leaves the screen, including through an
external remote. Playback status continues refreshing in the background. This
does not affect music playback or volume.
The screen uses an album-art-first widescreen layout with track details beside
the cover, playback controls and timeline along the bottom, and a heavily
blurred, dimmed artwork backdrop. The blur is generated and cached locally when
the current track changes using Kodi's bundled Pillow module; artwork is not
uploaded to an external service. The playlist is hidden by default and opens as
a right-side overlay from the top-right queue button; pressing Back or the
overlay's close button hides it again. The playlist opens around the current
track and loads additional tracks when you navigate to the top or bottom of the
loaded range. Selection continues into newly loaded tracks without rebuilding
the existing list when crossing a page boundary. Selecting a track plays it
without moving or recentering the list. When playback changes from the
Previous/Next controls or another app, the list selection follows the playing
track. Track details expand across the available right side when the playlist
overlay is hidden and contract to make room when it is open. The progress
timeline uses a dark gray unplayed section, with the
current playlist position and total count centered above it. The elapsed and
duration labels sit at the progress bar's vertical center.
The playlist heading uses the current playlist's name when Lyrion provides one,
and falls back to “Playlist” when the name is unavailable. Its count uses the
singular “track” for one item and “tracks” otherwise.
The screen header uses the Lyrion logo alongside the music library and player
names when available.

## Controls

Use the screen's Repeat, Previous, Play/Pause, Next, Shuffle, and Exit buttons
or select a track in the playlist overlay. Repeat cycles through Off, One, and
All; Shuffle toggles between Off and Songs. The repeat and shuffle
icons change with each mode and use the warm peach accent when enabled. The
combined button shows a green play icon while playing, a yellow pause icon
while paused, and a neutral play icon when stopped. Clicking it pauses active
playback or starts/resumes playback otherwise. The Exit button uses a power
icon and sits alongside the transport controls. The focused playlist track is
highlighted in warm peach text.
At the start and end of the playlist, further up/down presses keep selection at
that boundary; left/right navigation returns focus to the top-right playlist
button.
The current Lyrion volume appears at the top right in decibels relative to full
volume (100% is 0 dB; 0% is -inf dB). Kodi keyboard and remote navigation works
while this screen is open: left/right from the playlist return focus to the
top-right playlist button; up/down continue moving through the playlist. Select
activates a control, and the usual
previous, next, pause, and play actions trigger the corresponding Lyrion
commands. Volume Up and Volume Down send 5% volume changes to Lyrion while this
screen is open.
Kodi may also apply its own volume change to those same key actions.
Escape or Back closes the playlist overlay when it is open; otherwise it returns
to Kodi Home. The on-screen **Exit** button also returns to Kodi Home.

Kodi's built-in remote-app transport buttons are routed to the Kodi media
player, not arbitrary add-ons. A remote app that supports custom Kodi commands
can call the add-on directly with `RunPlugin`, for example:

```text
RunPlugin(plugin://plugin.audio.lyrion/?action=next)
```

The supported action values are `previous`, `next`, `play`, `pause`,
`volumeup`, and `volumedown`. The same commands can be assigned to IR or
keyboard buttons in Kodi's keymap, such as:

```xml
<keymap>
  <global>
    <remote>
      <skipnext>RunPlugin(plugin://plugin.audio.lyrion/?action=next)</skipnext>
      <skipprevious>RunPlugin(plugin://plugin.audio.lyrion/?action=previous)</skipprevious>
      <play>RunPlugin(plugin://plugin.audio.lyrion/?action=play)</play>
      <pause>RunPlugin(plugin://plugin.audio.lyrion/?action=pause)</pause>
    </remote>
  </global>
</keymap>
```
