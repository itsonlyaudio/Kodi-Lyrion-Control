import base64
import hashlib
import io
import json
import math
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs


ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
PLAYLIST_CONTROL_ID = 300
REFRESH_CONTROL_ID = 104
ACTION_NAV_BACK = 92
REPEAT_CONTROL_ID = 107
SHUFFLE_CONTROL_ID = 109
PLAYLIST_PAGE_SIZE = 100
GUI_SOUND_MODE_SETTING = "audiooutput.guisoundmode"
PROGRESS_LEFT = 60 + 140
PROGRESS_TOP = 35 + 850
PROGRESS_WIDTH = 1520
PROGRESS_HEIGHT = 14
WINDOW_ID = 11000
REFRESH_OWNER_PROPERTY = "LyrionRefreshOwner"
MAX_ARTWORK_BYTES = 20 * 1024 * 1024
BLUR_SOURCE_SIZE = (480, 270)
BLUR_RADIUS = 18


def parse_discovery_response(packet, address):
    if not packet.startswith(b"E"):
        return None

    fields = {}
    offset = 1
    while offset < len(packet):
        if len(packet) - offset < 5:
            return None
        key = packet[offset:offset + 4].decode("ascii", errors="ignore")
        value_length = packet[offset + 4]
        offset += 5
        if len(packet) - offset < value_length:
            return None
        try:
            fields[key] = packet[offset:offset + value_length].decode("utf-8")
        except UnicodeDecodeError:
            return None
        offset += value_length

    name = fields.get("NAME", "").strip()
    try:
        port = int(fields.get("JSON", "9000"))
    except ValueError:
        return None
    if not name or not 1 <= port <= 65535:
        return None
    return {"name": name, "host": address, "port": port}


def discover_servers(timeout=2.0):
    request_packet = b"eIPAD\0NAME\0JSON\0UUID\0VERS\0"
    servers = {}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.settimeout(0.25)
        sock.sendto(request_packet, ("255.255.255.255", 3483))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.25, remaining))
            try:
                packet, source = sock.recvfrom(4096)
            except socket.timeout:
                continue
            server = parse_discovery_response(packet, source[0])
            if server:
                servers[(server["host"], server["port"])] = server
    except OSError as exc:
        raise RuntimeError("Lyrion network discovery failed: {}.".format(exc)) from exc
    finally:
        if sock is not None:
            sock.close()
    return list(servers.values())


class LyrionClient:
    def __init__(self):
        configured_host = ADDON.getSetting("server_host").strip()
        port_value = ADDON.getSetting("server_port").strip()
        self.player_id = ADDON.getSetting("player_id").strip()
        username = ADDON.getSetting("username")
        password = ADDON.getSetting("password")

        # Kodi can return an empty value for an unset integer preference.
        port_value = port_value or "9000"
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError("The Lyrion server port must be a number.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("The Lyrion server port must be between 1 and 65535.")

        auto_discover_setting = ADDON.getSetting("auto_discover").strip().lower()
        auto_discover = auto_discover_setting != "false"
        if auto_discover:
            servers = discover_servers()
            if len(servers) == 1:
                host, port = servers[0]["host"], servers[0]["port"]
                xbmc.log(
                    "Lyrion: discovered {} at {}:{}".format(
                        servers[0]["name"], host, port
                    ),
                    xbmc.LOGINFO,
                )
            elif len(servers) > 1:
                labels = [
                    "{} ({})".format(server["name"], server["host"])
                    for server in servers
                ]
                selected = xbmcgui.Dialog().select("Select Lyrion server", labels)
                if selected < 0:
                    raise ValueError("No Lyrion server was selected.")
                host, port = servers[selected]["host"], servers[selected]["port"]
            elif configured_host:
                host = configured_host
                xbmc.log(
                    "Lyrion: no servers discovered; using configured host {}.".format(host),
                    xbmc.LOGWARNING,
                )
            else:
                raise RuntimeError(
                    "No Lyrion server was discovered. Check that Kodi and the server "
                    "are on the same network, or set the server host manually."
                )
        else:
            host = configured_host

        if not host or "/" in host or "://" in host:
            raise ValueError(
                "Enter a server host name or IP address, or enable automatic discovery."
            )
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        self.base_url = "http://{}:{}".format(host, port)
        self.endpoint = self.base_url + "/jsonrpc.js"
        self.auth_header = None
        if username:
            token = base64.b64encode(
                "{}:{}".format(username, password).encode("utf-8")
            ).decode("ascii")
            self.auth_header = "Basic " + token

        self.player_name = self.player_id
        if not self.player_id:
            self.player_id, self.player_name = self.select_player()
        else:
            try:
                self.player_name = self.find_player_name(self.player_id) or self.player_id
            except RuntimeError as exc:
                xbmc.log(
                    "Lyrion: could not resolve selected player name: {}".format(exc),
                    xbmc.LOGWARNING,
                )
        self.library_name = ""
        try:
            library = self.request(["pref", "libraryname", "?"])
            if isinstance(library, dict):
                self.library_name = str(library.get("_p2") or "").strip()
        except RuntimeError as exc:
            xbmc.log(
                "Lyrion: could not read the music library name: {}".format(exc),
                xbmc.LOGWARNING,
            )

    def request(self, command, player_id=None):
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [
                self.player_id if player_id is None else player_id,
                command,
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.auth_header:
            request.add_header("Authorization", self.auth_header)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Lyrion server returned HTTP {}.".format(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("Could not connect to the Lyrion server: {}.".format(exc)) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("The Lyrion server returned an invalid JSON response.") from exc

        if not isinstance(result, dict):
            raise RuntimeError("The Lyrion server returned an invalid JSON response.")
        if "error" in result:
            raise RuntimeError("Lyrion server error: {}.".format(result["error"]))
        return result.get("result", {})

    def get_players(self):
        result = self.request(
            ["players", "0", "50", "playerid", "name", "model"],
            player_id="",
        )
        players = result.get("players_loop", []) if isinstance(result, dict) else []
        if isinstance(players, dict):
            players = list(players.values())
        if not isinstance(players, list):
            players = []
        players = [
            player for player in players
            if isinstance(player, dict)
            and (player.get("playerid") or player.get("id"))
        ]
        return players

    def find_player_name(self, player_id):
        for player in self.get_players():
            candidate_id = str(player.get("playerid") or player.get("id"))
            if candidate_id.lower() == player_id.lower():
                return str(player.get("name") or player_id)
        return ""

    def select_player(self):
        players = self.get_players()
        if not players:
            raise RuntimeError(
                "No players were returned by the Lyrion server. Check that a "
                "player is connected, or enter its MAC address in the add-on settings."
            )

        if len(players) == 1:
            selected = 0
        else:
            labels = [
                "{} ({})".format(
                    player.get("name") or "Unnamed player",
                    player.get("playerid") or player.get("id"),
                )
                for player in players
            ]
            selected = xbmcgui.Dialog().select("Select Lyrion player", labels)
            if selected < 0:
                raise ValueError("No Lyrion player was selected.")

        player_id = str(
            players[selected].get("playerid") or players[selected].get("id")
        )
        ADDON.setSetting("player_id", player_id)
        return player_id, str(players[selected].get("name") or player_id)

    def status(self, start=0, count=PLAYLIST_PAGE_SIZE):
        result = self.request([
            "status",
            str(start),
            str(count),
            "tags:cgaldyrtK",
        ])
        if isinstance(result, dict):
            result["_playlist_start"] = start
        return result

    def playlist_duration(self, count):
        result = self.request(["status", "0", str(count), "tags:d"])
        tracks = get_playlist(result)
        if len(tracks) != count:
            raise RuntimeError(
                "The Lyrion server returned {} of {} playlist tracks for total time."
                .format(len(tracks), count)
            )
        total_duration = 0.0
        for index, track in enumerate(tracks):
            try:
                duration = float(track["duration"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(
                    "The Lyrion server did not return a valid duration for playlist "
                    "track {}.".format(index + 1)
                ) from exc
            if not math.isfinite(duration) or duration < 0:
                raise RuntimeError(
                    "The Lyrion server returned an invalid duration for playlist "
                    "track {}.".format(index + 1)
                )
            total_duration += duration
        return total_duration

    def volume(self):
        result = self.request(["mixer", "volume", "?"])
        if not isinstance(result, dict):
            raise RuntimeError("The Lyrion server returned an invalid volume response.")
        value = next(
            (
                result[key]
                for key in ("_p2", "_volume", "volume", "mixer_volume", "mixer volume")
                if key in result
            ),
            None,
        )
        try:
            volume = int(float(value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "The Lyrion server did not return a numeric volume percentage."
            ) from exc
        if not 0 <= volume <= 100:
            raise RuntimeError("The Lyrion server returned a volume outside 0-100%.")
        return volume

    def command(self, *parts):
        self.request(list(parts))

    def artwork_url(self, track):
        artwork = track.get("artwork_url")
        if isinstance(artwork, str) and artwork:
            if artwork.startswith(("http://", "https://")):
                return artwork
            return urllib.parse.urljoin(self.base_url, artwork)
        cover_id = track.get("coverid") or track.get("id")
        if cover_id is None:
            return ""
        return "{}/music/{}/cover.jpg".format(
            self.base_url, urllib.parse.quote(str(cover_id), safe="")
        )


def format_time(value):
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def format_playlist_time(value):
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    if hours:
        minutes, seconds = divmod(remainder, 60)
        return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return format_time(seconds)


def format_volume_db(volume):
    if volume == 0:
        return "-inf dB"
    return "{:.1f} dB".format(20 * math.log10(volume / 100.0))


def get_playlist(data):
    playlist = data.get("playlist_loop", [])
    if isinstance(playlist, dict):
        playlist = list(playlist.values())
    if not isinstance(playlist, list):
        return []
    return [item for item in playlist if isinstance(item, dict)]


def playlist_item_label(track, is_current):
    title = track.get("title") or "Unknown title"
    artist = track.get("artist") or ""
    if is_current:
        title = "[B]{}[/B]".format(title)
        artist = "[B]{}[/B]".format(artist) if artist else ""
    return "{}  -  {}".format(title, artist) if artist else title


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_handle, code, message, headers, url):
        redirected = super().redirect_request(
            request, file_handle, code, message, headers, url
        )
        if redirected is not None:
            original = urllib.parse.urlsplit(request.full_url)
            destination = urllib.parse.urlsplit(url)
            if (original.scheme, original.netloc) != (
                destination.scheme,
                destination.netloc,
            ):
                redirected.remove_header("Authorization")
        return redirected


def create_blurred_artwork(source_url, target_path, server_url, auth_header):
    from PIL import Image, ImageFilter

    headers = {}
    source = urllib.parse.urlsplit(source_url)
    server = urllib.parse.urlsplit(server_url)
    if source.scheme not in ("http", "https"):
        raise RuntimeError("Artwork URL must use HTTP or HTTPS.")
    if auth_header and (source.scheme, source.netloc) == (server.scheme, server.netloc):
        headers["Authorization"] = auth_header
    request = urllib.request.Request(source_url, headers=headers)
    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    with opener.open(request, timeout=10) as response:
        image_data = response.read(MAX_ARTWORK_BYTES + 1)
    if len(image_data) > MAX_ARTWORK_BYTES:
        raise RuntimeError("Artwork image exceeds the 20 MB processing limit.")

    try:
        with Image.open(io.BytesIO(image_data)) as source_image:
            if source_image.width * source_image.height > 40000000:
                raise RuntimeError("Artwork image dimensions are too large to process.")
            image = source_image.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise RuntimeError("The artwork image could not be decoded.") from exc
    image.thumbnail(
        BLUR_SOURCE_SIZE,
        getattr(Image, "Resampling", Image).LANCZOS,
    )
    image = image.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    image = image.resize(
        (1920, 1080),
        getattr(Image, "Resampling", Image).LANCZOS,
    )
    temporary_path = target_path + ".tmp"
    image.save(temporary_path, format="JPEG", quality=84, optimize=True)
    os.replace(temporary_path, target_path)


def kodi_jsonrpc(method, params):
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except (TypeError, ValueError, RuntimeError) as exc:
        raise RuntimeError("Kodi JSON-RPC {} request failed: {}.".format(method, exc)) from exc
    if not isinstance(response, dict):
        raise RuntimeError("Kodi returned an invalid JSON-RPC response for {}.".format(method))
    if "error" in response:
        raise RuntimeError(
            "Kodi JSON-RPC {} failed: {}.".format(method, response["error"])
        )
    return response.get("result")


class LyrionWindow(xbmcgui.WindowXML):
    def onInit(self):
        self.client = None
        self.original_gui_sound_mode = None
        self.player_window_id = None
        self.refresh_stop = threading.Event()
        self.refresh_thread = None
        self.refresh_owner_token = uuid.uuid4().hex
        self.kodi_window = xbmcgui.Window(10000)
        self.refresh_dispatch_count = 0
        self.refresh_click_count = 0
        self.playlist_items = []
        self.current_playlist_index = None
        self.playlist_tracks = {}
        self.playlist_start = 0
        self.playlist_total = 0
        self.playlist_summary_timestamp = None
        self.playlist_summary_loaded = False
        self.last_volume_warning_at = 0.0
        self.blur_artwork_url = None
        self.blur_completed_url = None
        self.blur_result = None
        self.blur_lock = threading.Lock()
        self.blur_thread = None
        self.setProperty("PlaylistVisible", "false")
        self.setProperty("RepeatMode", "0")
        self.setProperty("ShuffleMode", "0")
        progress_path = "{}/resources/skins/Default/1080i/progress-fill.png".format(
            ADDON.getAddonInfo("path")
        )
        self.progress_fill = xbmcgui.ControlImage(
            PROGRESS_LEFT,
            PROGRESS_TOP,
            1,
            PROGRESS_HEIGHT,
            progress_path,
        )
        self.addControl(self.progress_fill)
        self.progress_fill.setColorDiffuse("FFFFB092")
        self.progress_fill.setVisible(False)
        previous_owner = self.kodi_window.getProperty(REFRESH_OWNER_PROPERTY)
        self.kodi_window.setProperty(
            REFRESH_OWNER_PROPERTY, self.refresh_owner_token
        )
        if previous_owner:
            xbmc.log(
                "Lyrion: replacing refresh owner {}.".format(previous_owner[:8]),
                xbmc.LOGWARNING,
            )
        xbmc.log(
            "Lyrion: claimed refresh ownership {}.".format(
                self.refresh_owner_token[:8]
            ),
            xbmc.LOGINFO,
        )
        try:
            self.player_window_id = self.get_active_window_id()
        except RuntimeError as exc:
            self.player_window_id = None
            xbmc.log(
                "Lyrion: could not identify the player window during initialization: "
                "{}.".format(exc),
                xbmc.LOGWARNING,
            )
        if self.player_window_id is None:
            xbmc.log(
                "Lyrion: could not identify the player window during initialization; "
                "GUI sounds will remain muted until the window closes.",
                xbmc.LOGWARNING,
            )
        else:
            xbmc.log(
                "Lyrion: identified active player window ID {}.".format(
                    self.player_window_id
                ),
                xbmc.LOGINFO,
            )
        self.mute_gui_sounds()
        try:
            self.client = LyrionClient()
            self.refresh_status()
            self.start_refresh_loop()
        except (RuntimeError, ValueError) as exc:
            self.setProperty("Error", str(exc))

    def onClick(self, control_id):
        if control_id != 105 and not self.is_refresh_owner():
            return
        if control_id == 105:
            self.exit_addon()
            return
        if control_id == 106:
            self.toggle_playlist()
            return
        if control_id == 108:
            self.set_playlist_visible(False)
            return
        if self.client is None:
            xbmc.log(
                "Lyrion: ignored control {} because no server connection is available.".format(
                    control_id
                ),
                xbmc.LOGWARNING,
            )
            return
        if control_id == 100:
            self.send_command("playlist", "index", "-1")
        elif control_id == 101:
            if self.getProperty("Mode").lower() == "play":
                self.send_command("pause", "1")
            else:
                self.send_command("play")
        elif control_id == 103:
            self.send_command("playlist", "index", "+1")
        elif control_id == REPEAT_CONTROL_ID:
            self.cycle_playlist_mode("repeat")
        elif control_id == SHUFFLE_CONTROL_ID:
            self.cycle_playlist_mode("shuffle")
        elif control_id == REFRESH_CONTROL_ID:
            self.refresh_click_count += 1
            if self.refresh_click_count == 1 or self.refresh_click_count % 10 == 0:
                xbmc.log(
                    "Lyrion: refresh control received click #{}.".format(
                        self.refresh_click_count
                    ),
                    xbmc.LOGINFO,
                )
            self.refresh_status()
        elif control_id == PLAYLIST_CONTROL_ID:
            list_control = self.getControl(PLAYLIST_CONTROL_ID)
            item = list_control.getSelectedItem()
            if item is not None:
                try:
                    index = int(item.getProperty("playlist_index"))
                    self.send_command("playlist", "index", str(index))
                except ValueError:
                    self.setProperty("Error", "Could not select that playlist track.")

    def get_playlist_mode(self, data, mode, state_property):
        state = self.getProperty(state_property) or "0"
        keys = (
            "playlist {}".format(mode),
            "playlist_{}".format(mode),
            "_playlist_{}".format(mode),
        )
        value = next((data[key] for key in keys if key in data), state)
        try:
            mode_value = int(value)
        except (TypeError, ValueError, OverflowError):
            mode_value = -1
        if mode_value not in (0, 1, 2):
            xbmc.log(
                "Lyrion: ignoring invalid playlist {} mode {!r}.".format(
                    mode, value
                ),
                xbmc.LOGWARNING,
            )
            return int(state)
        return mode_value

    def set_playlist_mode_icon(self, mode, state):
        state_property = "RepeatMode" if mode == "repeat" else "ShuffleMode"
        self.setProperty(state_property, str(state))
        icon_directory = "{}/resources/skins/Default/1080i".format(
            ADDON.getAddonInfo("path")
        )
        icons = (
            ("repeat-off.png", "repeat-one.png", "repeat-all.png")
            if mode == "repeat"
            else ("shuffle-off.png", "shuffle-tracks.png", "shuffle-releases.png")
        )
        icon_property = "RepeatIcon" if mode == "repeat" else "ShuffleIcon"
        self.setProperty(
            icon_property,
            "{}/{}".format(icon_directory, icons[state]),
        )

    def cycle_playlist_mode(self, mode):
        state_property = "RepeatMode" if mode == "repeat" else "ShuffleMode"
        current_state = int(self.getProperty(state_property) or "0")
        next_state = (
            (current_state + 1) % 3
            if mode == "repeat"
            else (0 if current_state else 1)
        )
        if self.send_command("playlist", mode, str(next_state)):
            self.set_playlist_mode_icon(mode, next_state)

    def onAction(self, action):
        action_id = action.getId()
        if action_id not in (9, 10, ACTION_NAV_BACK) and not self.is_refresh_owner():
            return
        if action_id in (1, 2) and self.getFocusId() == PLAYLIST_CONTROL_ID:
            self.setFocusId(106)
        elif action_id in (3, 4) and self.getFocusId() == PLAYLIST_CONTROL_ID:
            control = self.getControl(PLAYLIST_CONTROL_ID)
            selected_position = control.getSelectedPosition()
            if action_id == 3 and selected_position == 0:
                if self.playlist_tracks and min(self.playlist_tracks) > 0:
                    self.load_playlist_page("previous")
            elif action_id == 4 and selected_position == len(self.playlist_items) - 1:
                if (
                    self.playlist_tracks
                    and max(self.playlist_tracks) + 1 < self.playlist_total
                ):
                    self.load_playlist_page("next")
        elif action_id == 14:
            self.onClick(103)
        elif action_id == 15:
            self.onClick(100)
        elif action_id == 12:
            self.send_command("pause", "1")
        elif action_id == 79:
            self.send_command("play")
        elif action_id == 117:
            self.onClick(101)
        elif action_id == 88:
            self.change_lyrion_volume("+5")
        elif action_id == 89:
            self.change_lyrion_volume("-5")
        elif action_id in (9, 10, ACTION_NAV_BACK):
            if self.getProperty("PlaylistVisible").lower() == "true":
                self.set_playlist_visible(False)
            else:
                self.exit_addon()

    def toggle_playlist(self):
        visible = self.getProperty("PlaylistVisible").lower() == "true"
        self.set_playlist_visible(not visible)

    def set_playlist_visible(self, visible):
        self.setProperty("PlaylistVisible", "true" if visible else "false")
        if not visible:
            self.playlist_summary_loaded = False
        self.setFocusId(PLAYLIST_CONTROL_ID if visible else 106)

    def update_blurred_artwork(self, artwork_url):
        if artwork_url != self.blur_artwork_url:
            self.blur_artwork_url = artwork_url
            self.blur_completed_url = None
            self.setProperty("BlurredArtwork", "")

        with self.blur_lock:
            result = self.blur_result
            self.blur_result = None
        if result is not None:
            result_url, result_path, error = result
            if result_url == self.blur_artwork_url:
                self.blur_completed_url = result_url
                if error is None:
                    self.setProperty("BlurredArtwork", result_path)
                else:
                    xbmc.log(
                        "Lyrion: could not create blurred artwork: {}".format(error),
                        xbmc.LOGWARNING,
                    )

        if (
            artwork_url
            and artwork_url != self.blur_completed_url
            and (self.blur_thread is None or not self.blur_thread.is_alive())
        ):
            cache_dir = xbmcvfs.translatePath(
                "special://profile/addon_data/plugin.audio.lyrion/blurred-artwork"
            )
            if not xbmcvfs.exists(cache_dir) and not xbmcvfs.mkdirs(cache_dir):
                raise RuntimeError("Could not create the blurred artwork cache.")
            image_name = hashlib.sha256(
                artwork_url.encode("utf-8")
            ).hexdigest() + ".jpg"
            image_path = os.path.join(cache_dir, image_name)
            if xbmcvfs.exists(image_path):
                self.blur_completed_url = artwork_url
                self.setProperty("BlurredArtwork", image_path)
                return

            self.blur_thread = threading.Thread(
                target=self.create_blurred_artwork_worker,
                args=(artwork_url, image_path),
                name="LyrionArtworkBlur",
                daemon=True,
            )
            self.blur_thread.start()

    def create_blurred_artwork_worker(self, artwork_url, image_path):
        error = None
        try:
            create_blurred_artwork(
                artwork_url,
                image_path,
                self.client.base_url,
                self.client.auth_header,
            )
        except (
            ImportError,
            OSError,
            RuntimeError,
            TimeoutError,
            urllib.error.URLError,
            ValueError,
        ) as exc:
            error = exc
        with self.blur_lock:
            self.blur_result = (artwork_url, image_path, error)

    def change_lyrion_volume(self, change):
        if self.client is None:
            message = "Cannot change Lyrion volume because no server connection is available."
            self.setProperty("Error", message)
            xbmc.log("Lyrion: " + message, xbmc.LOGWARNING)
            return
        try:
            self.client.command("mixer", "volume", change)
            xbmc.log(
                "Lyrion: sent volume change {}.".format(change),
                xbmc.LOGINFO,
            )
            self.setProperty("Error", "")
        except (RuntimeError, ValueError) as exc:
            self.setProperty("Error", str(exc))
            xbmc.log(
                "Lyrion: volume change {} failed: {}".format(change, exc),
                xbmc.LOGERROR,
            )

    def exit_addon(self):
        xbmc.log("Lyrion: Exit control activated.", xbmc.LOGINFO)
        self.restore_gui_sounds()
        self.release_refresh_ownership()
        xbmc.executebuiltin("ReplaceWindow(Home)")

    def mute_gui_sounds(self):
        try:
            response = kodi_jsonrpc(
                "Settings.GetSettingValue",
                {"setting": GUI_SOUND_MODE_SETTING},
            )
            original_mode = response.get("value") if isinstance(response, dict) else None
            if not isinstance(original_mode, int) or original_mode not in (0, 1, 2):
                raise RuntimeError("Kodi returned an invalid GUI sound mode.")
            if original_mode != 0:
                result = kodi_jsonrpc(
                    "Settings.SetSettingValue",
                    {"setting": GUI_SOUND_MODE_SETTING, "value": 0},
                )
                if result is not True:
                    raise RuntimeError("Kodi did not accept the GUI sound setting change.")
                self.original_gui_sound_mode = original_mode
        except RuntimeError as exc:
            xbmc.log(
                "Lyrion: could not mute Kodi GUI sounds: {}".format(exc),
                xbmc.LOGWARNING,
            )

    def restore_gui_sounds(self):
        if self.original_gui_sound_mode is None:
            return
        original_mode = self.original_gui_sound_mode
        try:
            result = kodi_jsonrpc(
                "Settings.SetSettingValue",
                {
                    "setting": GUI_SOUND_MODE_SETTING,
                    "value": original_mode,
                },
            )
            if result is not True:
                raise RuntimeError("Kodi did not accept the GUI sound setting restore.")
            self.original_gui_sound_mode = None
            xbmc.log("Lyrion: restored Kodi GUI sound mode {}.".format(original_mode), xbmc.LOGINFO)
        except RuntimeError as exc:
            xbmc.log(
                "Lyrion: could not restore Kodi GUI sounds: {}".format(exc),
                xbmc.LOGERROR,
            )

    def get_active_window_id(self):
        response = kodi_jsonrpc(
            "GUI.GetProperties",
            {"properties": ["currentwindow"]},
        )
        active_window = (
            response.get("currentwindow") if isinstance(response, dict) else None
        )
        if not isinstance(active_window, dict):
            raise RuntimeError("Kodi did not return the active window.")
        try:
            return int(active_window["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Kodi returned an invalid active window ID.") from exc

    def onClose(self):
        self.stop_refresh_loop()
        self.release_refresh_ownership()
        self.restore_gui_sounds()

    def is_refresh_owner(self):
        return (
            self.kodi_window.getProperty(REFRESH_OWNER_PROPERTY)
            == self.refresh_owner_token
        )

    def release_refresh_ownership(self):
        if self.is_refresh_owner():
            self.kodi_window.clearProperty(REFRESH_OWNER_PROPERTY)

    def start_refresh_loop(self):
        if self.refresh_thread is not None and self.refresh_thread.is_alive():
            return
        self.refresh_stop.clear()
        self.refresh_thread = threading.Thread(
            target=self.refresh_loop,
            name="LyrionPlaybackRefresh",
            daemon=True,
        )
        self.refresh_thread.start()
        xbmc.log("Lyrion: automatic refresh loop started.", xbmc.LOGINFO)

    def refresh_loop(self):
        next_refresh = time.monotonic() + 1.0
        while not self.refresh_stop.is_set():
            xbmc.sleep(50)
            if self.refresh_stop.is_set():
                break
            if not self.is_refresh_owner():
                xbmc.log(
                    "Lyrion: stopping refresh loop after ownership was transferred.",
                    xbmc.LOGINFO,
                )
                self.refresh_stop.set()
                break
            if time.monotonic() < next_refresh:
                continue
            window_is_active = self.player_window_id is None
            if self.player_window_id is not None:
                try:
                    window_is_active = (
                        self.get_active_window_id() == self.player_window_id
                    )
                except RuntimeError as exc:
                    xbmc.log(
                        "Lyrion: could not identify the active Kodi window: {}".format(
                            exc
                        ),
                        xbmc.LOGWARNING,
                    )
            if not window_is_active:
                self.restore_gui_sounds()
            elif self.original_gui_sound_mode is None:
                self.mute_gui_sounds()
            try:
                xbmc.executebuiltin(
                    "SendClick({},{})".format(WINDOW_ID, REFRESH_CONTROL_ID),
                    True,
                )
                self.refresh_dispatch_count += 1
                if (
                    self.refresh_dispatch_count == 1
                    or self.refresh_dispatch_count % 10 == 0
                ):
                    xbmc.log(
                        "Lyrion: dispatched refresh click #{}.".format(
                            self.refresh_dispatch_count
                        ),
                        xbmc.LOGINFO,
                    )
            except RuntimeError as exc:
                xbmc.log(
                    "Lyrion: automatic refresh click failed: {}".format(exc),
                    xbmc.LOGERROR,
                )
            next_refresh = time.monotonic() + 1.0

    def stop_refresh_loop(self):
        self.refresh_stop.set()

    def send_command(self, *command):
        if self.client is None:
            message = "Cannot send a Lyrion command because no server connection is available."
            self.setProperty("Error", message)
            xbmc.log("Lyrion: " + message, xbmc.LOGWARNING)
            return False
        try:
            self.client.command(*command)
            self.setProperty("Error", "")
            return True
        except (RuntimeError, ValueError) as exc:
            self.setProperty("Error", str(exc))
            xbmc.log("Lyrion: command failed: {}".format(exc), xbmc.LOGWARNING)
            return False

    def refresh_status(self):
        if self.client is None:
            return
        try:
            data = self.client.status(self.playlist_start)
            if not isinstance(data, dict):
                raise RuntimeError(
                    "The Lyrion server returned an unexpected status response."
                )
            try:
                current_index = int(data.get("playlist_cur_index", -1))
            except (TypeError, ValueError):
                current_index = -1
            if (
                current_index >= 0
                and current_index not in self.playlist_tracks
                and (
                    current_index < self.playlist_start
                    or current_index >= self.playlist_start + len(get_playlist(data))
                )
            ):
                try:
                    total = max(
                        0, int(data.get("playlist_tracks", current_index + 1))
                    )
                except (TypeError, ValueError):
                    total = current_index + 1
                start = max(
                    0,
                    min(
                        current_index - PLAYLIST_PAGE_SIZE // 2,
                        max(0, total - PLAYLIST_PAGE_SIZE),
                    ),
                )
                data = self.client.status(start)
                if not isinstance(data, dict):
                    raise RuntimeError(
                        "The Lyrion server returned an unexpected status response."
                    )
                self.playlist_tracks.clear()
                self.playlist_start = start
            self.update_ui(data)
            try:
                volume = self.client.volume()
            except RuntimeError as exc:
                self.setProperty("Volume", "Volume unavailable")
                now = time.monotonic()
                if now - self.last_volume_warning_at >= 60:
                    xbmc.log(
                        "Lyrion: could not read current volume: {}".format(exc),
                        xbmc.LOGWARNING,
                    )
                    self.last_volume_warning_at = now
            else:
                self.setProperty("Volume", "Volume {}".format(format_volume_db(volume)))
            self.setProperty("Error", "")
        except (RuntimeError, ValueError) as exc:
            self.setProperty("Error", str(exc))
            xbmc.log(
                "Lyrion: playback refresh failed: {}".format(exc),
                xbmc.LOGWARNING,
            )

    def load_playlist_page(self, direction):
        if self.client is None or not self.playlist_tracks or self.playlist_total <= 0:
            return
        if direction == "previous":
            first_index = min(self.playlist_tracks)
            if first_index <= 0:
                return
            start = max(0, first_index - PLAYLIST_PAGE_SIZE)
            count = first_index - start
        elif direction == "next":
            last_index = max(self.playlist_tracks)
            start = last_index + 1
            if start >= self.playlist_total:
                return
            count = min(PLAYLIST_PAGE_SIZE, self.playlist_total - start)
        else:
            raise ValueError("Unknown playlist page direction: {}.".format(direction))

        try:
            data = self.client.status(start, count)
            page_tracks = get_playlist(data)
            if direction == "previous":
                target_index = start + len(page_tracks) - 1
            else:
                target_index = start
            self.update_ui(data, selection_index=target_index)
            self.playlist_start = min(self.playlist_tracks)
        except (RuntimeError, ValueError) as exc:
            self.setProperty("Error", str(exc))
            xbmc.log(
                "Lyrion: could not load the {} playlist page: {}".format(
                    direction, exc
                ),
                xbmc.LOGWARNING,
            )

    def update_ui(self, data, selection_index=None):
        if not isinstance(data, dict):
            raise RuntimeError("The Lyrion server returned an unexpected status response.")
        playlist = get_playlist(data)
        try:
            page_start = max(0, int(data.get("_playlist_start", self.playlist_start)))
        except (TypeError, ValueError):
            page_start = self.playlist_start
        for offset, track in enumerate(playlist):
            self.playlist_tracks[page_start + offset] = track
        try:
            current_index = int(data.get("playlist_cur_index", -1))
        except (TypeError, ValueError):
            current_index = -1
        current = self.playlist_tracks.get(current_index, {})
        title = current.get("title") or "Nothing playing"
        artist = current.get("artist") or ""
        album = current.get("album") or ""
        mode = data.get("mode", "stop")
        try:
            position = max(0.0, float(data.get("time", 0)))
        except (TypeError, ValueError, OverflowError):
            position = 0.0
        try:
            duration = max(0.0, float(current.get("duration", data.get("duration", 0))))
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        progress = min(100.0, position * 100.0 / duration) if duration else 0.0

        self.setProperty("Title", str(title))
        self.setProperty("Artist", str(artist))
        self.setProperty("Album", str(album))
        self.setProperty("Renderer", str(self.client.player_name))
        library_name = str(self.client.library_name or "").strip()
        player_name = str(self.client.player_name or "").strip()
        if library_name and player_name:
            header = "{} playing on {}".format(library_name, player_name)
        else:
            header = library_name or player_name
        self.setProperty("Header", header)
        playlist_name = str(data.get("playlist_name") or "").strip()
        self.setProperty("PlaylistName", playlist_name or "Playlist")
        try:
            self.playlist_total = max(
                0, int(data.get("playlist_tracks", len(self.playlist_tracks)))
            )
        except (TypeError, ValueError):
            self.playlist_total = len(self.playlist_tracks)
        self.setProperty("PlaylistCount", str(self.playlist_total))
        self.setProperty(
            "PlaylistCountLabel", "track" if self.playlist_total == 1 else "tracks"
        )
        playlist_timestamp = data.get("playlist_timestamp")
        if self.playlist_total == 0:
            self.setProperty("PlaylistSummary", "0 tracks · 0:00")
            self.playlist_summary_timestamp = playlist_timestamp
            self.playlist_summary_loaded = True
        elif (
            self.getProperty("PlaylistVisible").lower() == "true"
            and (
                not self.playlist_summary_loaded
                or playlist_timestamp != self.playlist_summary_timestamp
            )
        ):
            try:
                total_duration = self.client.playlist_duration(self.playlist_total)
            except RuntimeError as exc:
                self.setProperty(
                    "PlaylistSummary",
                    "{} {} · time unavailable".format(
                        self.playlist_total,
                        "track" if self.playlist_total == 1 else "tracks",
                    ),
                )
                xbmc.log(
                    "Lyrion: could not calculate playlist total time: {}".format(exc),
                    xbmc.LOGWARNING,
                )
            else:
                self.setProperty(
                    "PlaylistSummary",
                    "{} {} · {}".format(
                        self.playlist_total,
                        "track" if self.playlist_total == 1 else "tracks",
                        format_playlist_time(total_duration),
                    ),
                )
            self.playlist_summary_timestamp = playlist_timestamp
            self.playlist_summary_loaded = True
        playlist_position = (
            "{} of {}".format(current_index + 1, self.playlist_total)
            if 0 <= current_index < self.playlist_total
            else ""
        )
        self.setProperty("PlaylistPosition", playlist_position)
        repeat_mode = self.get_playlist_mode(data, "repeat", "RepeatMode")
        shuffle_mode = self.get_playlist_mode(data, "shuffle", "ShuffleMode")
        self.set_playlist_mode_icon("repeat", repeat_mode)
        self.set_playlist_mode_icon("shuffle", shuffle_mode)
        self.setProperty("Mode", str(mode))
        mode = str(mode).lower()
        if mode == "play":
            play_pause_label = "[B][COLOR FF00FF00]▶[/COLOR][/B]"
        elif mode in ("pause", "paused"):
            play_pause_label = "[B][COLOR FFFFCC00]||[/COLOR][/B]"
        else:
            play_pause_label = "▶"
        self.setProperty("PlayPauseButtonLabel", play_pause_label)
        self.setProperty("Position", format_time(position))
        self.setProperty("Duration", format_time(duration))
        fill_width = round(PROGRESS_WIDTH * progress / 100.0)
        if fill_width > 0:
            self.progress_fill.setWidth(fill_width)
            self.progress_fill.setVisible(duration > 0)
        else:
            self.progress_fill.setVisible(False)
        artwork_url = self.client.artwork_url(current)
        self.setProperty("Artwork", artwork_url)
        self.update_blurred_artwork(artwork_url)
        visible_tracks = sorted(self.playlist_tracks.items())
        snapshot = [
            (index, str(track.get("title", "Unknown title")),
             str(track.get("artist", "")),
             str(track.get("album", "")))
            for index, track in visible_tracks
        ]
        playlist_changed = snapshot != self.playlist_items
        current_track_changed = current_index != self.current_playlist_index
        selected_index = None
        control = self.getControl(PLAYLIST_CONTROL_ID)
        try:
            selected_item = control.getSelectedItem()
            if selected_item is not None:
                selected_index = int(selected_item.getProperty("playlist_index"))
        except (RuntimeError, TypeError, ValueError):
            pass
        if playlist_changed:
            can_append = (
                current_index == self.current_playlist_index
                and len(snapshot) > len(self.playlist_items)
                and snapshot[:len(self.playlist_items)] == self.playlist_items
            )
            if not can_append:
                control.reset()
            tracks_to_add = (
                visible_tracks[len(self.playlist_items):]
                if can_append
                else visible_tracks
            )
            for index, track in tracks_to_add:
                item = xbmcgui.ListItem(
                    label=playlist_item_label(track, index == current_index)
                )
                item.setProperty("playlist_index", str(index))
                control.addItem(item)
            self.playlist_items = snapshot
        elif current_track_changed:
            positions = {
                index: position for position, (index, _) in enumerate(visible_tracks)
            }
            for index in {self.current_playlist_index, current_index}:
                if index in positions:
                    control.getListItem(positions[index]).setLabel(
                        playlist_item_label(
                            self.playlist_tracks[index], index == current_index
                        )
                    )
        if selection_index is not None:
            target_index = selection_index
        elif current_track_changed:
            target_index = current_index
        else:
            target_index = selected_index
        if target_index not in self.playlist_tracks:
            target_index = current_index
        if (
            playlist_changed
            or current_track_changed
            or selection_index is not None
        ) and (
            target_index in self.playlist_tracks
        ):
            control.selectItem(
                next(
                    position for position, (index, _) in enumerate(visible_tracks)
                    if index == target_index
                )
            )
        self.current_playlist_index = current_index


def run_player():
    window = LyrionWindow(
        "LyrionWindow.xml",
        ADDON.getAddonInfo("path"),
        "Default",
        "1080i",
    )
    window.doModal()
    del window


def main():
    query = urllib.parse.parse_qs(sys.argv[2].lstrip("?")) if len(sys.argv) > 2 else {}
    action = query.get("action", [""])[0]
    commands = {
        "previous": ("playlist", "index", "-1"),
        "next": ("playlist", "index", "+1"),
        "play": ("play",),
        "pause": ("pause", "1"),
        "volumeup": ("mixer", "volume", "+5"),
        "volumedown": ("mixer", "volume", "-5"),
        "volumeup": ("mixer", "volume", "+5"),
        "volumedown": ("mixer", "volume", "-5"),
    }
    if action:
        try:
            if action not in commands:
                raise ValueError("Unknown Lyrion action: {}.".format(action))
            LyrionClient().command(*commands[action])
        except (RuntimeError, ValueError) as exc:
            xbmcgui.Dialog().notification(
                "Lyrion", str(exc), xbmcgui.NOTIFICATION_ERROR
            )
        return

    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, cacheToDisc=False)
    run_player()


if __name__ == "__main__":
    main()
