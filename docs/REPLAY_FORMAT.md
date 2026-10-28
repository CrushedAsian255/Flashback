# Flashback Replay File Format

Reverse-documented from the Flashback source (mod version 0.43.6, Minecraft 26.3, commit `bf046b7`).
Every claim below cites the class that implements it. All paths are relative to
`src/main/java/com/moulberry/flashback/`.

---

## 1. Overview

A Flashback replay is a **ZIP archive** (`.zip`) holding:

| Entry | Required | What it is |
|---|---|---|
| `metadata.json` | yes | JSON index: replay name, UUID, versions, markers, and the ordered list of segment files with their durations |
| `c0.flashback`, `c1.flashback`, … | yes (≥1) | **Segments** ("replay chunks"). Each is a self-contained binary stream: header + snapshot + tick-by-tick actions |
| `level_chunk_caches/0`, `/1`, … | usually | Deduplicated world-chunk packets, 10 000 per file, referenced by index from segments |
| `level_chunk_cache` | legacy | Older single-file form of the chunk cache (only read by the replay combiner) |
| `icon.png` | optional | 64×64 thumbnail |

The design in one sentence: **recorded clientbound network packets, plus a few Flashback-specific
"actions", grouped into 5-minute segments, where each segment starts with a full world snapshot so playback
can seek to any segment without replaying everything before it.**

```
replay.zip
├── metadata.json
├── icon.png
├── level_chunk_caches/
│   ├── 0          ← chunk packets #0..9999
│   └── 1          ← chunk packets #10000..19999
├── c0.flashback   ← ticks [0, d0)
├── c1.flashback   ← ticks [d0, d0+d1)
└── c2.flashback   ← …
```

Where replays live: `<game dir>/flashback/replays/*.zip` (`Flashback.getReplayFolder()`).
Editor data (keyframes, camera paths, etc.) is **not** in the zip — see §9.

---

## 2. Primitive encodings

All binary data is written with Netty `ByteBuf` / Minecraft `FriendlyByteBuf`, so:

| Type | Encoding |
|---|---|
| `int` | 4 bytes, **big-endian**, signed |
| `long` | 8 bytes, big-endian |
| `float` / `double` | IEEE-754, big-endian (4 / 8 bytes) |
| `boolean` | 1 byte (0/1) |
| `byte` | 1 byte, signed |
| `VarInt` | Minecraft VarInt: 7 data bits per byte, little-endian groups, high bit = "more bytes follow", max 5 bytes |
| `String` / `Identifier` | VarInt byte length + UTF-8 bytes. Identifiers are `namespace:path` |
| `ResourceKey` | Written as its Identifier |
| `UUID` | two longs (most-significant, least-significant) |
| `GameProfile` | Minecraft `ByteBufCodecs.GAME_PROFILE` (UUID, name, property list) |
| Minecraft packet | VarInt packet ID + packet body, using the vanilla codec for that protocol phase (§6) |

Packets are encoded with a `RegistryFriendlyByteBuf`, so registry references (blocks, items, …) are numeric IDs
that only make sense against the registries sent in the segment's snapshot. This is why replays are
version-locked (§10).

---

## 3. `metadata.json`

Written by `record/FlashbackMeta.toJson()`; read by `FlashbackMeta.fromJson()`.

### 3.1 Example

```json
{
  "uuid": "6f1b0c1e-8e0e-4a39-9f3e-4c7a2f0d5b11",
  "name": "My Replay",
  "version_string": "26.3",
  "world_name": "Hypixel",
  "data_version": 4671,
  "protocol_version": 775,
  "bobby_world_name": "mc.hypixel.net",
  "total_ticks": 12345,
  "markers": {
    "200":  { "colour": 16711680, "description": "Boss fight" },
    "6000": { "colour": 11141290, "description": "Changed Dimension" },
    "9001": { "colour": 65280, "position": { "position": [12.5, 64.0, -3.0], "dimension": "minecraft:overworld" } }
  },
  "distantHorizonPaths": { "minecraft:overworld": "/abs/path/on/recording/machine/..." },
  "customNamespacesForRegistries": {
    "minecraft:block": ["somemod"],
    "minecraft:item":  ["somemod"]
  },
  "modVersions": { "minecraft": "26.3", "fabricloader": "0.17.0", "flashback": "0.43.6" },
  "chunks": {
    "c0.flashback": { "duration": 6000, "forcePlaySnapshot": false },
    "c1.flashback": { "duration": 6000, "forcePlaySnapshot": false },
    "c2.flashback": { "duration": 345,  "forcePlaySnapshot": false }
  }
}
```
(Values are illustrative.)

### 3.2 Fields

| Key | Type | Required on read | Meaning / source |
|---|---|---|---|
| `uuid` | string UUID | **yes** | Random per recording. Links the replay to its editor-state file (§9). Regenerated when combining. |
| `name` | string | **yes** | Display name. Default `"Unnamed"`; set when saving; editable in the "Edit replay" screen (rewrites this file inside the zip). |
| `version_string` | string | no | `FabricLoader.getRawGameVersion()` at recording time |
| `world_name` | string | no | Server list name, or singleplayer world name. Omitted if it was the default ("Minecraft Server" / "New World") |
| `data_version` | int | no | Minecraft world data version. Omitted when 0. Combining requires these to match. |
| `protocol_version` | int | no | Network protocol version. Omitted when 0 |
| `bobby_world_name` | string | no | Only when the Bobby mod is loaded |
| `total_ticks` | int | no | Total recorded ticks. Omitted if ≤ 0. Informational (UI); playback recomputes length from chunk durations |
| `markers` | object | no | Key = tick (as string), value = marker (§3.3). Unparseable entries are silently skipped |
| `distantHorizonPaths` | object | no | Dimension → absolute filesystem path of Distant Horizons data **on the recording machine**. The LOD data itself is not in the zip |
| `customNamespacesForRegistries` | object | no | For each built-in registry, the non-`minecraft`/non-`brigadier` namespaces present. Used to warn about mod mismatches on load (`RegistryMetaHelper`) |
| `modVersions` | object | no | Top-level mod ID → version. Used to log mod changes on load (`ModListHelper`) |
| `chunks` | object | **yes** | **Ordered** map: segment filename → segment meta (§3.4). Insertion order = playback order |

If `uuid`, `name`, or `chunks` is missing, or any chunk entry lacks `duration`, the whole file is rejected
(`fromJson` returns `null`).

### 3.3 Marker object (`record/ReplayMarker`)

Serialized by Gson (`FlashbackGson.COMPRESSED`), so field names come from the Java record and `null` fields are omitted:

```json
{ "colour": <int RGB>, "position": { "position": [x, y, z], "dimension": "<dimension id>" }, "description": "<text>" }
```
- `colour` — 24-bit RGB int (e.g. `0xAA00AA` = 11141290 is used for automatic "Changed Dimension" markers).
- `position` — optional; `position` is a `Vector3f` serialized as a 3-element array (`serialization/Vector3fTypeAdapater`).
- `description` — optional.
- The map key is the tick at which the marker was created (`Recorder.addMarker` uses `writtenTicks`).

### 3.4 Segment meta (`record/FlashbackChunkMeta`)

| Key | Type | Meaning |
|---|---|---|
| `duration` | int (required) | Number of `next_tick` actions in the segment = ticks it covers |
| `forcePlaySnapshot` | bool (default false) | When playback *crosses into* this segment normally, re-apply its snapshot. Set on the first segment of the second replay when two replays are combined (§8), because the world state there is discontinuous |

Segment start ticks are the running sum of previous durations (`ReplayServer` constructor builds
`playableChunksByStart`).

---

## 4. Segment file (`cN.flashback`) binary layout

Written by `io/ReplayWriter`, read by `io/ReplayReader`.

```
+--------------------------------------------------------------+
| int     magic = 0xD780E884                                   |  Flashback.MAGIC
| VarInt  actionCount                                          |
| Identifier actionName[0..actionCount-1]                      |  the action table
| int     snapshotSize   (bytes)                               |
| byte[snapshotSize]  snapshot: sequence of Action records     |
| ... Action records until end of file ...                     |  the tick stream
+--------------------------------------------------------------+

Action record:
| VarInt  actionId     (index into this file's action table)   |
| int     payloadSize  (bytes, big-endian)                     |
| byte[payloadSize] payload                                    |
```

Key details:

- **The action table is per file.** IDs are indices into that file's table, not global constants. The writer
  emits every registered action in registration order (§5), but a reader must map by name.
- **Every segment re-writes the header.** After a segment is flushed, `ReplayWriter.popBytes()` resets
  and writes magic + table again, so each `cN.flashback` is independently parseable.
- **Snapshot size is back-patched.** The writer emits placeholder `0xDEADBEEF`, then overwrites it with the
  real size at `endSnapshot()`. Same trick for each action's `payloadSize`.
- **Snapshot may be empty** (`snapshotSize = 0`) — happens after the recording is unpaused (§7.4).
- **No end marker / no footer.** The stream ends at EOF.
- **Strict payload consumption.** The reader wraps each payload in a slice; if the handler doesn't read every
  byte, playback aborts with "failed to fully read".
- **Unknown actions:** if an action name isn't registered, the reader skips it **only if its path ends in
  `optional`** (using `payloadSize`). Otherwise it throws `Missing action`. This is the format's forward-compat rule.
- `next_tick` is forbidden inside a snapshot (`ReplayServer.handleNextTick` throws).

### 4.1 Tick semantics

There are no timestamps. Time is defined purely by `next_tick` actions:

- Actions before the first `next_tick` in the stream happen at the segment's start tick.
- Each `next_tick` advances the replay by one tick (1/20 s at normal speed).
- `duration` in metadata must equal the number of `next_tick`s; playback errors out if the stream runs
  past the declared duration, or ends before it.

---

## 5. Actions

Registered in `Flashback.java` (order defines the table order the writer emits):

| # | Identifier | Class | Payload |
|---|---|---|---|
| 0 | `flashback:action/next_tick` | `ActionNextTick` | empty (size 0) |
| 1 | `flashback:action/game_packet` | `ActionGamePacket` | one PLAY-phase clientbound packet |
| 2 | `flashback:action/configuration_packet` | `ActionConfigurationPacket` | one CONFIGURATION-phase clientbound packet |
| 3 | `flashback:action/create_local_player` | `ActionCreateLocalPlayer` | local player spawn data |
| 4 | `flashback:action/move_entities` | `ActionMoveEntities` | batched entity positions |
| 5 | `flashback:action/level_chunk_cached` | `ActionLevelChunkCached` | VarInt index into chunk cache |
| 6 | `flashback:action/accurate_player_position_optional` | `ActionAccuratePlayerPosition` | sub-tick player camera positions |
| 7 | `flashback:action/real_time_clock_optional` | `ActionRealTimeClock` | wall-clock timestamp / delta |
| (8) | `flashback:action/simple_voice_chat_sound_optional` | `compat/simple_voice_chat/ActionSimpleVoiceChatSound` | voice audio; only registered when Simple Voice Chat is installed |

Because the voice-chat action ends in `optional`, a replay containing it still plays without that mod.

### 5.1 `next_tick`
No payload. Advances `currentTick`, flushes pending entity spawns, and rolls the real-time clock forward.

### 5.2 `game_packet`
```
VarInt  packetId     (GameProtocols.CLIENTBOUND for the recording's protocol version)
...     packet body  (vanilla codec)
```
Decoded with `GameProtocols.CLIENTBOUND_TEMPLATE` bound to the current registry access.
Notes from `ReplayServer.handleGamePacket`:
- If decoding fails, the packet is skipped (error is logged a limited number of times) rather than aborting.
- `ClientboundCustomPayloadPacket` (mod channels) is **not** re-encoded: the raw bytes after the packet ID are
  forwarded as `FlashbackRawCustomPayload`, because some mods don't round-trip encode/decode. Payload IDs on the
  user's "ignored custom payloads" config list are dropped.
- Level chunk packets are never written as `game_packet` — they always become `level_chunk_cached` (§6.3).

### 5.3 `configuration_packet`
```
VarInt  packetId     (ConfigurationProtocols.CLIENTBOUND)
...     packet body
```
Used for registry data, tags, feature flags, and resource packs (both in snapshots and when a server
re-enters configuration mid-session).

### 5.4 `create_local_player`
Written right after every `ClientboundLoginPacket` (`Recorder.writeCreateLocalPlayer`):
```
UUID     uuid
double   x, y, z
float    xRot (pitch)
float    yRot (yaw)
float    yHeadRot
double   velocityX, velocityY, velocityZ
GameProfile profile   (properties = merged from session profile + in-game profile, so skins survive)
VarInt   gameModeId
```
The recording player isn't sent to themselves by the server, so this action spawns them as a normal
player entity in the replay (ID = the login packet's player ID). If the entity already exists, it's
teleported instead.

### 5.5 `move_entities`
Written once per tick when any entity moved (`Recorder.writeEntityPositions`):
```
VarInt dimensionCount            (recorder always writes 1)
repeat dimensionCount:
    ResourceKey dimension        (e.g. minecraft:overworld)
    VarInt entityCount
    repeat entityCount:
        VarInt  entityId
        double  x, y, z          (entity.trackingPosition())
        float   yaw, pitch, headYaw   (wrapped to [-180, 180))
        boolean onGround
```
- Only entities whose (pos, yaw, pitch, headYaw, onGround) changed since last tick are included.
- Skipped entities: removed, ender-dragon parts, and types with client tracking range ≤ 0
  (`PacketHelper.shouldIgnoreEntity`).
- This replaces vanilla movement packets: `ClientboundMoveEntityPacket.*`, `ClientboundRotateHeadPacket`,
  `ClientboundPlayerPositionPacket`, etc. are excluded from recording (`IgnoredPacketSet`). Positions are the
  client's interpolated view, so playback matches what the recorder saw, including the local player.
- On playback, unknown entity IDs get a `ClientboundEntityPositionSyncPacket` broadcast instead.

### 5.6 `level_chunk_cached`
```
VarInt cacheIndex
```
Points at chunk packet `#cacheIndex` in the chunk cache (§6). Playback skips re-sending if the loaded chunk
already came from the same index (tracked per chunk via `LevelChunkExt.flashback$getCachedChunkId`), except while
exporting video.

### 5.7 `accurate_player_position_optional`
Only written when config `recording.localPlayerUpdatesPerSecond > 20` (smooth first-person camera):
```
VarInt entityId                       (local player)
VarInt count                          (= updatesPerSecond / 20 + 1)
repeat count:
    double x, y, z
    float  yaw, pitch
```
These are positions sampled at render-frame partial ticks and interpolated to evenly spaced sub-tick points
(`Recorder.writeAccurateFirstPersonPosition`). Emission stops after 10 consecutive ticks without movement.
Playback forwards it as a custom payload; users can disable it (`advanced.disableIncreasedFirstPersonUpdates`).

### 5.8 `real_time_clock_optional`
Records wall-clock time so the editor can show real time and map ticks to real time:
```
byte delta
if delta == 0:
    long epochMillis          (absolute timestamp)
```
- Expected time per tick is 50 ms. `delta = actual − (previous + 50)`, as a signed byte.
- If `delta` is 0 the action is **not written** (unless a sync is forced).
- If it doesn't fit in a signed byte, or a sync is forced (start of recording, each new snapshot), an absolute
  value is written with `delta = 0`.
- Playback: `pending = (pending or current) + 50 + delta`, applied on the next `next_tick`.

### 5.9 `simple_voice_chat_sound_optional`
```
UUID   source
VarInt sampleCount
short  samples[sampleCount]      (raw PCM, big-endian shorts)
byte   type
  type 0 (static):      — nothing else
  type 1 (locational):  double x, y, z; float distance
  type 2 (entity):      boolean whispering; float distance
```
(`packet/FlashbackVoiceChatSound`). Ignored during snapshot processing; muted while fast-forwarding or paused.

---

## 6. Chunk cache (`level_chunk_caches/N`)

World chunks are the bulk of a replay, and the same chunk is often re-sent unchanged (every snapshot, every
time you walk back). Flashback deduplicates them globally across the recording.

### 6.1 File layout
Each file is a plain concatenation, no header:
```
repeat:
    int  size                (big-endian)
    byte packet[size]        (VarInt packetId + ClientboundLevelChunkWithLightPacket body)
```
Read until EOF (`playback/ReplayChunkCache.loadLevelChunkCache`). A truncated tail is logged and ignored.

### 6.2 Indexing
- `CHUNK_CACHE_SIZE = 10000`.
- Global index `i` lives in file `level_chunk_caches/(i / 10000)` at position `i % 10000`.
- Indices are global for the whole replay, shared across all segments.
- Loaded lazily, held in `SoftReference`s (can be evicted under memory pressure and reloaded).

### 6.3 Deduplication (`CachedChunkPacket`, `AsyncReplaySaver.writeGamePackets`)
For each `ClientboundLevelChunkWithLightPacket` about to be recorded:
1. Hash with SHA-512 (fallback SHA-256): chunk x, z, raw section data, encoded light data, and block entities
   (sorted by `(y << 8) | packedXZ` so order doesn't matter; includes type ID and NBT).
2. Look up by a 64-bit hash folded from the digest; confirm with x, z, and full digest equality.
3. Match → reuse the old index. New → assign next index, append to the current cache file.
4. Write a `level_chunk_cached` action with that index.

Cache files are appended to on disk as recording progresses (they're not segment-scoped).

### 6.4 Legacy `level_chunk_cache`
A single-file version of the same format (index `0..`) from older Flashback versions. The exporter still copies
it if present, and `ReplayCombiner` reads it. **Note:** current playback (`ReplayChunkCache`) only looks under
`level_chunk_caches/`, not the legacy file.

---

## 7. What gets recorded, and when

### 7.1 Segment boundaries (`Recorder.endTick`)
A segment is closed and a new one started when any of these is true:
- `CHUNK_LENGTH_SECONDS * 20` = **6000 ticks (5 min)** written in this segment,
- the player changed dimension (also adds a purple "Changed Dimension" marker if `recording.markDimensionChanges`),
- a configuration phase finished (server sent the client back through configuration, e.g. proxy server switch),
- the recording was unpaused,
- recording stopped.

If the segment would otherwise be empty, or the current tick wasn't written, one extra `next_tick` is appended
so no segment has zero length. `metadata.json` is rewritten each time a segment is flushed (previous version kept as
`metadata.json.old` in the temp folder).

### 7.2 Snapshot contents (`Recorder.writeSnapshot`)
The snapshot rebuilds the full client state from scratch. In order:

**Configuration packets** (`configuration_packet`):
1. `ClientboundUpdateEnabledFeaturesPacket` — enabled feature flags
2. `ClientboundRegistryDataPacket` × N — every synchronized registry (biomes, dimension types, damage types, …)
3. `ClientboundUpdateTagsPacket` — all registry tags
4. `ClientboundResourcePackPopPacket` (clear) then `ClientboundResourcePackPushPacket` per server resource pack (URL + hash)

**Game packets / actions:**
5. Real-time clock sync (absolute)
6. `ClientboundLoginPacket` — player ID, hardcore flag, dimension list, render/simulation distance, spawn info (dimension type, dimension, hashed seed, game mode, flat/debug, sea level)
7. `create_local_player`
8. `ClientboundPlayerInfoUpdatePacket` — tab list entries (listed players, all online players, and every player entity in the world), preferring entries with skin properties
9. `ClientboundTabListPacket` — header/footer
10. `ClientboundBossEventPacket` (add) per boss bar
11. Scoreboard: teams, displayed objectives, display slots, scores
12. World border, time (`ClientboundSetTimePacket` incl. clock updates), default spawn, rain start/stop, rain level, thunder level
13. All loaded chunks (light updates are forced to finish first), **sorted nearest-to-player first**, as `level_chunk_cached`. Optionally includes Bobby's fake chunks (`recording.recordBobbyIntoReplay`).
14. If `recording.recordHotbar`: experience, health/food, selected slot, hotbar slots 0–8
15. Per entity: `ClientboundAddEntityPacket` (not for the local player), non-default entity data, attributes, equipment, passengers/vehicle, leash link
16. `ClientboundMapItemDataPacket` per known map (full 128×128 patch + decorations)
17. `writeCustomSnapshot` hook (empty; mods can mixin to append packets)

### 7.3 Per-tick stream
Every client tick while recording (and not paused, in a level, not on a loading screen):

1. **Start of tick:** `real_time_clock_optional` (if needed)
2. **End of tick:**
   - All clientbound packets received this tick (filtered, §7.5), in arrival order, as `game_packet` / `configuration_packet`. A `create_local_player` follows any `ClientboundLoginPacket` (respawn/dimension change).
   - `move_entities`
   - Local player state, synthesized from the client (the server never sends these to the player about themselves): `SetEntityData` diffs, `SetEquipment` diffs, `BlockDestruction` (mining progress), `SwingAnimation`, `SetEntityMotion` (always), and — if hotbar recording is on — `SetExperience`, `SetHealth`, `SetHeldSlot`, `ContainerSetSlot` (container 0, slot 0–8) diffs
   - `accurate_player_position_optional` (if enabled)
   - `next_tick`
3. **Any time:** voice-chat actions (via `submitCustomTask`)

The icon is captured ~20 ticks in (when no screen is open), center-cropped and scaled to 64×64.

### 7.4 Pausing
While paused, nothing is written. On unpause, the current segment is closed and the next one gets an
**empty snapshot**, then the snapshot packets are written as ordinary actions at the start of the stream.
(This makes the world "pop" back to current state during normal playback, but seeking into that segment still works.)

### 7.5 Packet filtering and rewriting (`Recorder.writePacketAsync`, `record/IgnoredPacketSet`)
- `ClientboundBundlePacket` is flattened into its sub-packets.
- `ClientboundPlayerChatPacket` is converted to `ClientboundSystemChatPacket` (decorated text), removing chat signatures. `ClientboundDeleteChatPacket` is dropped.
- Custom payloads on `fabric-screen-handler-api*` channels are dropped.
- Server-sent `SetEntityData`/`SetEquipment` for the local player are dropped (replaced by synthesized diffs above).
- Sounds and level events (`ClientboundSoundPacket`, `ClientboundSoundEntityPacket`, `ClientboundLevelEventPacket`) are **not taken from the network**; they're captured client-side by `mixin/record/MixinClientLevel` / `MixinLocalPlayer` so locally-predicted sounds/effects are included.
- Dropped entirely (`IGNORED`): cookies, custom report details, server links, disconnect, ping/keep-alive/pong, transfer, dialogs, finish-configuration, code of conduct, stats, recipe book/recipes, sign editor, entity move/rotate/head packets, player position, minecart move, container open/close/content/data/slot, forget-chunk, abilities, cursor item, inventory, experience/health (synthesized instead), tick-rate state/step, combat, camera, cooldowns, advancements, commands/suggestions, tag query, books, screens, merchant offers, chunk-cache radius/center, simulation distance, block-change acks, chat completions, start-configuration, chunk batch start/finish, debug packets, waypoints, game-rule values, low-disk warnings.

### 7.6 Recording pipeline (for context)
`Recorder` (game thread) → `AsyncReplaySaver` queue (1024 tasks) → background thread owning one `ReplayWriter`.
Files go to a temp folder (`TempFolderProvider`, type RECORDING): `metadata.json`, `metadata.json.old`,
`cN.flashback`, `level_chunk_caches/N`, `icon.png`. If the game crashes, folders that contain `metadata.json`
are offered for recovery on next launch.

---

## 8. Saving, editing, combining

### 8.1 Export to zip (`record/ReplayExporter`)
1. Read `metadata.json` (fallback `metadata.json.old`); apply the chosen name.
2. Drop any `chunks` entries whose file is missing; abort if none remain.
3. Write zip (Deflate, `BEST_SPEED`) in this order: `metadata.json` (pretty-printed), `level_chunk_caches/*`, legacy `level_chunk_cache`, `icon.png`, each segment in metadata order.
4. Delete the temp folder.

Default filename: replay name with spaces → underscores, or the date/time, made unique; UUID as fallback.

### 8.2 Renaming (`screen/EditReplayScreen`)
Opens the zip as a `FileSystem` and overwrites `/metadata.json` with the new `name`. Nothing else changes.

### 8.3 Combining two replays (`io/ReplayCombiner`)
1. Requires equal `data_version`.
2. Loads both chunk caches into one list; builds old-index → new-index maps for each replay.
3. Metadata: copies the first replay's; new random `uuid`; new name; second replay's markers shifted by the first's `total_ticks`; `total_ticks` summed.
4. Second replay's segments are renamed to continue numbering (`c{n}.flashback`); its first segment gets `forcePlaySnapshot: true`.
5. Rewrites chunk cache files from the merged list.
6. Copies `icon.png` from the first replay.
7. For each segment: copies header, then re-encodes every `level_chunk_cached` payload with the remapped index (recomputing payload and snapshot sizes); other actions are copied byte-for-byte. Segments with no `level_chunk_cached` in their table are copied unchanged.

Metadata is written compact (not pretty) here.

---

## 9. Data stored outside the zip

| Location | Content |
|---|---|
| `<game dir>/flashback/editor_states/<replay uuid>.json` (+ `.json.old` backup) | Editor state: scenes, keyframe tracks (camera, FOV, time-of-day, tickrate, block overrides, audio, …), export ranges, visibility/skin/name overrides, visual settings, timeline zoom, `usedByPaths`. Gson JSON, autosaved every 30 s when dirty. Matched to replays via `metadata.json` `uuid`. Unused states are cleaned up automatically. |
| Distant Horizons folders | Referenced by absolute path in `distantHorizonPaths`; not copied |
| `config/flashback/` | Mod config, incl. recent replays list |

Consequence: sharing just the `.zip` shares the recording, **not** the camera work. Copying the zip keeps the
same UUID, so both copies share one editor state on the same machine.

---

## 10. Compatibility rules

- **Version-locked packets.** `game_packet` / `configuration_packet` / chunk cache entries use the protocol's numeric
  packet IDs and registry IDs. A replay plays back reliably only on the same Minecraft protocol version
  (the mod targets one MC version per build).
- **Mods.** Modded registry entries are embedded via registry data packets; `customNamespacesForRegistries` and
  `modVersions` exist to warn when the playback mod set differs. Lenient registry mixins (`mixin/playback/lenient_registry`) try to tolerate missing entries.
- **New action types** must end in `optional` if older readers should skip them.
- **Readers must use the per-file action table**, never hard-coded IDs.

---

## 11. Writing your own reader — checklist

1. Open zip, parse `metadata.json`, iterate `chunks` **in JSON order**, accumulating `duration` to get each segment's start tick.
2. For each segment: check magic `0xD780E884`, read action table, read `snapshotSize`, split snapshot vs stream.
3. Walk records: `VarInt id`, `int size`, `size` bytes. Skip unknown actions (ideally only `*optional`).
4. Count `next_tick` to track time; verify against `duration`.
5. For `level_chunk_cached`, resolve via `level_chunk_caches/(i/10000)` entry `i%10000` (each entry = `int size` + packet).
6. To decode packets, you need a Minecraft protocol library for the recording's `protocol_version`, with registries initialized from the snapshot's configuration packets.

A minimal, tested container-level inspector is in [`docs/flashback_inspect.py`](flashback_inspect.py)
(`python3 flashback_inspect.py replay.zip`). It prints metadata, each segment's action table, per-action counts
(snapshot vs stream), and checks `next_tick` counts against `duration`.

### 11.1 Worked byte example
A tiny segment with the standard 8-action table, a snapshot containing one absolute RTC and one cached chunk,
then two ticks:

```
D7 80 E8 84                        magic
08                                 8 actions
1A "flashback:action/next_tick"    (VarInt len 26 + bytes)
... 7 more identifiers ...
00 00 00 14                        snapshotSize = 20 (14 + 6)
  07 00 00 00 09 00 <8-byte ms>    real_time_clock_optional, size 9: delta=0, epochMillis
  05 00 00 00 01 00                level_chunk_cached, size 1: index 0
00 00 00 00 00                     next_tick, size 0
07 00 00 00 01 03                  real_time_clock_optional, size 1: delta=+3 ms
00 00 00 00 00                     next_tick
```
→ metadata `duration` must be 2.
