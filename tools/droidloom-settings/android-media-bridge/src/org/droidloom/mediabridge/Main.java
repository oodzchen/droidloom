/* Copyright 2026 Droidloom. SPDX-License-Identifier: GPL-3.0-or-later */
package org.droidloom.mediabridge;

import android.content.ContentResolver;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Parcel;
import android.os.ResultReceiver;
import android.os.SystemClock;
import android.util.Base64;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Lightweight Android MediaSession -> JSON stdout/stdin bridge running under app_process (UID 2000).
 * Reports active media sessions, metadata, cover art PNGs, live playback state, volume, and
 * MediaSessionCompat repeat/shuffle modes, and dispatches MPRIS2 control commands back to Android.
 */
public final class Main {
    private static final String COMPAT_CMD_GET_EXTRA_BINDER =
            "android.support.v4.media.session.command.GET_EXTRA_BINDER";
    private static final String COMPAT_KEY_EXTRA_BINDER =
            "android.support.v4.media.session.EXTRA_BINDER";
    private static final String COMPAT_IFACE_SESSION =
            "android.support.v4.media.session.IMediaSession";
    private static final String COMPAT_IFACE_CALLBACK =
            "android.support.v4.media.session.IMediaControllerCallback";
    private static final String COMPAT_ACTION_SET_REPEAT_MODE =
            "android.support.v4.media.session.action.SET_REPEAT_MODE";
    private static final String COMPAT_ARG_REPEAT_MODE =
            "android.support.v4.media.session.action.ARGUMENT_REPEAT_MODE";
    private static final String COMPAT_ACTION_SET_SHUFFLE_MODE =
            "android.support.v4.media.session.action.SET_SHUFFLE_MODE";
    private static final String COMPAT_ARG_SHUFFLE_MODE =
            "android.support.v4.media.session.action.ARGUMENT_SHUFFLE_MODE";

    private static final int TX_REGISTER_CALLBACK = IBinder.FIRST_CALL_TRANSACTION + 2;
    private static final int TX_UNREGISTER_CALLBACK = IBinder.FIRST_CALL_TRANSACTION + 3;
    private static final int TX_GET_REPEAT_MODE = IBinder.FIRST_CALL_TRANSACTION + 36;
    private static final int TX_GET_SHUFFLE_MODE = IBinder.FIRST_CALL_TRANSACTION + 46;
    private static final int CB_ON_REPEAT_MODE_CHANGED = IBinder.FIRST_CALL_TRANSACTION + 8;
    private static final int CB_ON_SHUFFLE_MODE_CHANGED = IBinder.FIRST_CALL_TRANSACTION + 11;

    private static final int MAX_ART_PX = 512;
    private static final long EMIT_DEBOUNCE_MS = 50L;
    private static final long VOLUME_POLL_MS = 2000L;

    private final Context context;
    private final Handler mainHandler;
    private final MediaSessionManager sessionManager;
    private final AudioManager audioManager;

    private final Map<String, TrackedSession> trackedByPkg = new LinkedHashMap<>();
    private final Map<String, String> lastSentArtHash = new HashMap<>();

    private boolean emitPending;
    private boolean volumePollScheduled;
    private int lastMusicVolume = -1;
    private volatile boolean stopping;

    private final Runnable emitRunnable = new Runnable() {
        @Override
        public void run() {
            emitPending = false;
            emitSnapshot();
        }
    };

    private final Runnable volumePollRunnable = new Runnable() {
        @Override
        public void run() {
            if (stopping || trackedByPkg.isEmpty()) {
                volumePollScheduled = false;
                return;
            }
            int vol = safeStreamVolume();
            if (vol != lastMusicVolume) {
                lastMusicVolume = vol;
                scheduleEmit();
            }
            mainHandler.postDelayed(this, VOLUME_POLL_MS);
        }
    };

    private final MediaSessionManager.OnActiveSessionsChangedListener sessionsListener =
            new MediaSessionManager.OnActiveSessionsChangedListener() {
                @Override
                public void onActiveSessionsChanged(List<MediaController> controllers) {
                    syncControllers(controllers);
                    scheduleEmit();
                }
            };

    private final class TrackedSession {
        final String packageName;
        final MediaController controller;
        final MediaController.Callback callback;
        IBinder extraBinder;
        Binder compatCallback;
        boolean compatModeSupported;
        int repeatMode = -1;
        int shuffleMode = -1;

        TrackedSession(String packageName, MediaController controller) {
            this.packageName = packageName;
            this.controller = controller;
            this.callback = new MediaController.Callback() {
                @Override
                public void onPlaybackStateChanged(PlaybackState state) {
                    scheduleEmit();
                }

                @Override
                public void onMetadataChanged(MediaMetadata metadata) {
                    scheduleEmit();
                }

                @Override
                public void onAudioInfoChanged(MediaController.PlaybackInfo info) {
                    scheduleEmit();
                }

                @Override
                public void onExtrasChanged(Bundle extras) {
                    scheduleEmit();
                }

                @Override
                public void onSessionDestroyed() {
                    detach();
                    trackedByPkg.remove(TrackedSession.this.packageName, TrackedSession.this);
                    scheduleEmit();
                }
            };
        }

        void attach() {
            controller.registerCallback(callback, mainHandler);
            requestCompatExtraBinder();
        }

        void detach() {
            try {
                controller.unregisterCallback(callback);
            } catch (Throwable ignored) {
            }
            if (extraBinder != null && compatCallback != null) {
                Parcel data = Parcel.obtain();
                Parcel reply = Parcel.obtain();
                try {
                    data.writeInterfaceToken(COMPAT_IFACE_SESSION);
                    data.writeStrongBinder(compatCallback);
                    extraBinder.transact(TX_UNREGISTER_CALLBACK, data, reply, 0);
                    reply.readException();
                } catch (Throwable ignored) {
                } finally {
                    data.recycle();
                    reply.recycle();
                }
            }
            extraBinder = null;
            compatCallback = null;
        }

        private void requestCompatExtraBinder() {
            try {
                controller.sendCommand(
                        COMPAT_CMD_GET_EXTRA_BINDER,
                        null,
                        new ResultReceiver(mainHandler) {
                            @Override
                            protected void onReceiveResult(int resultCode, Bundle resultData) {
                                if (stopping || resultData == null) {
                                    return;
                                }
                                if (trackedByPkg.get(packageName) != TrackedSession.this) {
                                    return;
                                }
                                IBinder binder = resultData.getBinder(COMPAT_KEY_EXTRA_BINDER);
                                if (binder != null) {
                                    bindExtra(binder);
                                }
                            }
                        });
            } catch (Throwable ignored) {
            }
        }

        private void bindExtra(IBinder binder) {
            try {
                String desc = binder.getInterfaceDescriptor();
                if (!COMPAT_IFACE_SESSION.equals(desc)) {
                    return;
                }
                extraBinder = binder;
                int rep = queryInt(binder, TX_GET_REPEAT_MODE);
                int shuf = queryInt(binder, TX_GET_SHUFFLE_MODE);
                if (rep >= 0 || shuf >= 0) {
                    compatModeSupported = true;
                    repeatMode = rep;
                    shuffleMode = shuf;
                }
                compatCallback = new Binder() {
                    @Override
                    protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
                        try {
                            if (code == CB_ON_REPEAT_MODE_CHANGED) {
                                data.enforceInterface(COMPAT_IFACE_CALLBACK);
                                int next = data.readInt();
                                mainHandler.post(() -> {
                                    if (trackedByPkg.get(packageName) == TrackedSession.this) {
                                        compatModeSupported = true;
                                        repeatMode = next;
                                        scheduleEmit();
                                    }
                                });
                                return true;
                            }
                            if (code == CB_ON_SHUFFLE_MODE_CHANGED) {
                                data.enforceInterface(COMPAT_IFACE_CALLBACK);
                                int next = data.readInt();
                                mainHandler.post(() -> {
                                    if (trackedByPkg.get(packageName) == TrackedSession.this) {
                                        compatModeSupported = true;
                                        shuffleMode = next;
                                        scheduleEmit();
                                    }
                                });
                                return true;
                            }
                            if (code >= IBinder.FIRST_CALL_TRANSACTION
                                    && code <= IBinder.LAST_CALL_TRANSACTION) {
                                return true;
                            }
                            return super.onTransact(code, data, reply, flags);
                        } catch (Throwable ignored) {
                            return true;
                        }
                    }
                };
                Parcel data = Parcel.obtain();
                Parcel reply = Parcel.obtain();
                try {
                    data.writeInterfaceToken(COMPAT_IFACE_SESSION);
                    data.writeStrongBinder(compatCallback);
                    binder.transact(TX_REGISTER_CALLBACK, data, reply, 0);
                    reply.readException();
                } finally {
                    data.recycle();
                    reply.recycle();
                }
                scheduleEmit();
            } catch (Throwable ignored) {
            }
        }

        private int queryInt(IBinder binder, int txCode) {
            Parcel data = Parcel.obtain();
            Parcel reply = Parcel.obtain();
            try {
                data.writeInterfaceToken(COMPAT_IFACE_SESSION);
                if (!binder.transact(txCode, data, reply, 0)) {
                    return -1;
                }
                reply.readException();
                return reply.readInt();
            } catch (Throwable ignored) {
                return -1;
            } finally {
                data.recycle();
                reply.recycle();
            }
        }
    }

    private Main(Context context) {
        this.context = context;
        this.mainHandler = new Handler(Looper.getMainLooper());
        this.sessionManager = context.getSystemService(MediaSessionManager.class);
        this.audioManager = context.getSystemService(AudioManager.class);
        this.lastMusicVolume = safeStreamVolume();
    }

    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        Context ctx = createShellContext();
        Main app = new Main(ctx);
        app.start();
        Looper.loop();
        System.exit(0);
    }

    private static Context createShellContext() throws Exception {
        Class<?> atClass = Class.forName("android.app.ActivityThread");
        try {
            Method initMainline = atClass.getDeclaredMethod("initializeMainlineModules");
            initMainline.setAccessible(true);
            initMainline.invoke(null);
        } catch (Throwable ignored) {
        }
        Method systemMain = atClass.getMethod("systemMain");
        Object at = systemMain.invoke(null);
        Method getSystemContext = atClass.getMethod("getSystemContext");
        Context sysCtx = (Context) getSystemContext.invoke(at);
        try {
            Class<?> mfpi = Class.forName("android.media.MediaFrameworkPlatformInitializer");
            Method getMgr = mfpi.getMethod("getMediaServiceManager");
            if (getMgr.invoke(null) == null) {
                Class<?> msmClass = Class.forName("android.media.MediaServiceManager");
                Object msm = msmClass.getConstructor().newInstance();
                Method setMgr = mfpi.getMethod("setMediaServiceManager", msmClass);
                setMgr.invoke(null, msm);
            }
        } catch (Throwable ignored) {
        }
        try {
            return sysCtx.createPackageContext(
                    "com.android.shell", Context.CONTEXT_IGNORE_SECURITY);
        } catch (Throwable ignored) {
            return sysCtx;
        }
    }

    private void start() throws Exception {
        sessionManager.addOnActiveSessionsChangedListener(sessionsListener, null, mainHandler);
        List<MediaController> initial = sessionManager.getActiveSessions(null);
        syncControllers(initial);
        emitLine(new JSONObject().put("event", "ready"));
        emitSnapshot();

        Thread stdinThread = new Thread(this::readStdinLoop, "droidloom-media-stdin");
        stdinThread.setDaemon(true);
        stdinThread.start();
    }

    private void readStdinLoop() {
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while (!stopping && (line = reader.readLine()) != null) {
                String trimmed = line.trim();
                if (trimmed.isEmpty()) {
                    continue;
                }
                try {
                    JSONObject cmd = new JSONObject(trimmed);
                    mainHandler.post(() -> handleCommand(cmd));
                } catch (Throwable ignored) {
                }
            }
        } catch (Throwable ignored) {
        }
        if (!stopping) {
            mainHandler.post(this::shutdown);
        }
    }

    private void shutdown() {
        if (stopping) {
            return;
        }
        stopping = true;
        mainHandler.removeCallbacks(emitRunnable);
        mainHandler.removeCallbacks(volumePollRunnable);
        try {
            sessionManager.removeOnActiveSessionsChangedListener(sessionsListener);
        } catch (Throwable ignored) {
        }
        for (TrackedSession session : trackedByPkg.values()) {
            session.detach();
        }
        trackedByPkg.clear();
        Looper.getMainLooper().quitSafely();
    }

    private void scheduleEmit() {
        if (stopping || emitPending) {
            return;
        }
        emitPending = true;
        mainHandler.postDelayed(emitRunnable, EMIT_DEBOUNCE_MS);
    }

    private void syncControllers(List<MediaController> controllers) {
        Map<String, MediaController> desired = new LinkedHashMap<>();
        if (controllers != null) {
            for (MediaController controller : controllers) {
                if (controller == null) {
                    continue;
                }
                String pkg = controller.getPackageName();
                if (pkg == null || pkg.isEmpty() || desired.containsKey(pkg)) {
                    continue;
                }
                if ("com.android.server.telecom".equals(pkg)
                        && controller.getPlaybackState() == null
                        && controller.getMetadata() == null) {
                    continue;
                }
                desired.put(pkg, controller);
            }
        }

        Iterator<Map.Entry<String, TrackedSession>> it = trackedByPkg.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, TrackedSession> entry = it.next();
            MediaController next = desired.get(entry.getKey());
            if (next == null
                    || !next.getSessionToken().equals(entry.getValue().controller.getSessionToken())) {
                entry.getValue().detach();
                it.remove();
            }
        }

        Map<String, TrackedSession> reordered = new LinkedHashMap<>();
        for (Map.Entry<String, MediaController> entry : desired.entrySet()) {
            String pkg = entry.getKey();
            TrackedSession existing = trackedByPkg.get(pkg);
            if (existing == null) {
                existing = new TrackedSession(pkg, entry.getValue());
                existing.attach();
            }
            reordered.put(pkg, existing);
        }
        trackedByPkg.clear();
        trackedByPkg.putAll(reordered);

        if (!trackedByPkg.isEmpty() && !volumePollScheduled) {
            volumePollScheduled = true;
            mainHandler.postDelayed(volumePollRunnable, VOLUME_POLL_MS);
        } else if (trackedByPkg.isEmpty() && volumePollScheduled) {
            volumePollScheduled = false;
            mainHandler.removeCallbacks(volumePollRunnable);
        }
    }

    private int safeStreamVolume() {
        if (audioManager == null) {
            return 0;
        }
        try {
            return audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
        } catch (Throwable ignored) {
            return 0;
        }
    }

    private void emitSnapshot() {
        if (stopping) {
            return;
        }
        try {
            JSONArray arr = new JSONArray();
            for (TrackedSession session : trackedByPkg.values()) {
                arr.put(encodeSession(session));
            }
            JSONObject root = new JSONObject();
            root.put("event", "sessions");
            root.put("now_elapsed_ms", SystemClock.elapsedRealtime());
            root.put("sessions", arr);
            emitLine(root);
        } catch (Throwable ignored) {
        }
    }

    private JSONObject encodeSession(TrackedSession session) throws Exception {
        MediaController c = session.controller;
        PlaybackState ps = c.getPlaybackState();
        MediaMetadata md = c.getMetadata();
        MediaController.PlaybackInfo pi = c.getPlaybackInfo();

        int state = ps != null ? ps.getState() : PlaybackState.STATE_NONE;
        String status = mapPlaybackStatus(state);
        long posMs = ps != null ? Math.max(0L, ps.getPosition()) : 0L;
        long updatedMs = ps != null ? ps.getLastPositionUpdateTime() : 0L;
        double speed = ps != null && ps.getPlaybackSpeed() > 0f ? ps.getPlaybackSpeed() : 1.0;
        long actions = ps != null ? ps.getActions() : 0L;

        String title = "";
        String artist = "";
        String album = "";
        String albumArtist = "";
        String mediaId = "";
        long durationMs = 0L;
        long trackNumber = 0L;
        if (md != null) {
            title = firstNonEmpty(
                    md.getString(MediaMetadata.METADATA_KEY_TITLE),
                    md.getString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE));
            artist = firstNonEmpty(
                    md.getString(MediaMetadata.METADATA_KEY_ARTIST),
                    md.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST),
                    md.getString(MediaMetadata.METADATA_KEY_AUTHOR),
                    md.getString(MediaMetadata.METADATA_KEY_DISPLAY_SUBTITLE));
            album = firstNonEmpty(
                    md.getString(MediaMetadata.METADATA_KEY_ALBUM),
                    md.getString(MediaMetadata.METADATA_KEY_DISPLAY_DESCRIPTION));
            albumArtist = safeString(md.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST));
            mediaId = safeString(md.getString(MediaMetadata.METADATA_KEY_MEDIA_ID));
            durationMs = Math.max(0L, md.getLong(MediaMetadata.METADATA_KEY_DURATION));
            trackNumber = Math.max(0L, md.getLong(MediaMetadata.METADATA_KEY_TRACK_NUMBER));
        }

        ArtData art = extractArt(session.packageName, md);

        int volCur = 0;
        int volMax = 0;
        boolean volRemote = false;
        if (pi != null && pi.getPlaybackType() == MediaController.PlaybackInfo.PLAYBACK_TYPE_REMOTE
                && pi.getMaxVolume() > 0) {
            volRemote = true;
            volCur = Math.max(0, pi.getCurrentVolume());
            volMax = Math.max(1, pi.getMaxVolume());
        } else if (audioManager != null) {
            volCur = Math.max(0, audioManager.getStreamVolume(AudioManager.STREAM_MUSIC));
            volMax = Math.max(1, audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC));
        }
        double volumeNorm = volMax > 0 ? Math.max(0.0, Math.min(1.0, (double) volCur / volMax)) : 1.0;

        JSONArray customActions = new JSONArray();
        String modeAction = "";
        if (ps != null && ps.getCustomActions() != null) {
            for (PlaybackState.CustomAction ca : ps.getCustomActions()) {
                if (ca == null) {
                    continue;
                }
                String act = safeString(ca.getAction());
                String name = ca.getName() != null ? ca.getName().toString() : "";
                customActions.put(new JSONObject().put("action", act).put("name", name));
                if (modeAction.isEmpty() && isPlayModeKeyword(act, name)) {
                    modeAction = act;
                }
            }
        }

        JSONObject obj = new JSONObject();
        obj.put("package", session.packageName);
        obj.put("tag", safeString(c.getTag()));
        obj.put("status", status);
        obj.put("state", state);
        obj.put("position_ms", posMs);
        obj.put("updated_elapsed_ms", updatedMs);
        obj.put("speed", speed);
        obj.put("actions", actions);
        obj.put("can_play", (actions & (PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PLAY_PAUSE)) != 0);
        obj.put("can_pause", (actions & (PlaybackState.ACTION_PAUSE | PlaybackState.ACTION_PLAY_PAUSE)) != 0);
        obj.put("can_stop", (actions & PlaybackState.ACTION_STOP) != 0);
        obj.put("can_next", (actions & PlaybackState.ACTION_SKIP_TO_NEXT) != 0);
        obj.put("can_previous", (actions & PlaybackState.ACTION_SKIP_TO_PREVIOUS) != 0);
        obj.put("can_seek", (actions & PlaybackState.ACTION_SEEK_TO) != 0);
        obj.put("can_set_speed", (actions & PlaybackState.ACTION_SET_PLAYBACK_SPEED) != 0);
        obj.put("title", title);
        obj.put("artist", artist);
        obj.put("album", album);
        obj.put("album_artist", albumArtist);
        obj.put("duration_ms", durationMs);
        obj.put("track_number", trackNumber);
        obj.put("media_id", mediaId);
        obj.put("art_hash", art.hash);
        if (!art.pngB64.isEmpty()) {
            obj.put("art_png_b64", art.pngB64);
        }
        obj.put("art_uri", art.remoteUri);
        obj.put("volume", volumeNorm);
        obj.put("volume_current", volCur);
        obj.put("volume_max", volMax);
        obj.put("volume_remote", volRemote);
        obj.put("compat_mode_supported", session.compatModeSupported);
        obj.put("repeat_mode", session.repeatMode);
        obj.put("shuffle_mode", session.shuffleMode);
        obj.put("custom_actions", customActions);
        obj.put("mode_action", modeAction);
        return obj;
    }

    private static final class ArtData {
        final String hash;
        final String pngB64;
        final String remoteUri;

        ArtData(String hash, String pngB64, String remoteUri) {
            this.hash = hash;
            this.pngB64 = pngB64;
            this.remoteUri = remoteUri;
        }
    }

    private ArtData extractArt(String pkg, MediaMetadata md) {
        if (md == null) {
            return new ArtData("", "", "");
        }
        Bitmap bmp = null;
        for (String key : new String[] {
                MediaMetadata.METADATA_KEY_ALBUM_ART,
                MediaMetadata.METADATA_KEY_ART,
                MediaMetadata.METADATA_KEY_DISPLAY_ICON}) {
            bmp = md.getBitmap(key);
            if (bmp != null) {
                break;
            }
        }
        String remoteUri = "";
        boolean decodedLocal = false;
        if (bmp == null) {
            for (String key : new String[] {
                    MediaMetadata.METADATA_KEY_ALBUM_ART_URI,
                    MediaMetadata.METADATA_KEY_ART_URI,
                    MediaMetadata.METADATA_KEY_DISPLAY_ICON_URI}) {
                String uriStr = safeString(md.getString(key));
                if (uriStr.isEmpty()) {
                    continue;
                }
                if (uriStr.startsWith("content://") || uriStr.startsWith("file://")) {
                    Bitmap decoded = decodeLocalUri(uriStr);
                    if (decoded != null) {
                        bmp = decoded;
                        decodedLocal = true;
                        break;
                    }
                } else if (uriStr.startsWith("http://") || uriStr.startsWith("https://")) {
                    remoteUri = uriStr;
                    break;
                }
            }
        }
        if (bmp == null) {
            return new ArtData("", "", remoteUri);
        }

        Bitmap scaled = null;
        try {
            int w = bmp.getWidth();
            int h = bmp.getHeight();
            Bitmap src = bmp;
            if (w > 0 && h > 0 && Math.max(w, h) > MAX_ART_PX) {
                double factor = (double) MAX_ART_PX / Math.max(w, h);
                int nw = Math.max(1, (int) Math.round(w * factor));
                int nh = Math.max(1, (int) Math.round(h * factor));
                scaled = Bitmap.createScaledBitmap(bmp, nw, nh, true);
                src = scaled;
            }
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            src.compress(Bitmap.CompressFormat.PNG, 90, baos);
            byte[] png = baos.toByteArray();
            String hash = sha256Hex(png);
            String prev = lastSentArtHash.get(pkg);
            if (hash.equals(prev)) {
                return new ArtData(hash, "", remoteUri);
            }
            lastSentArtHash.put(pkg, hash);
            String b64 = Base64.encodeToString(png, Base64.NO_WRAP);
            return new ArtData(hash, b64, remoteUri);
        } catch (Throwable ignored) {
            return new ArtData("", "", remoteUri);
        } finally {
            if (scaled != null && scaled != bmp) {
                scaled.recycle();
            }
            if (decodedLocal) {
                bmp.recycle();
            }
        }
    }

    private Bitmap decodeLocalUri(String uriStr) {
        try {
            ContentResolver cr = context.getContentResolver();
            try (InputStream is = cr.openInputStream(Uri.parse(uriStr))) {
                if (is == null) {
                    return null;
                }
                return BitmapFactory.decodeStream(is);
            }
        } catch (Throwable ignored) {
            return null;
        }
    }

    private void handleCommand(JSONObject cmd) {
        String op = cmd.optString("cmd", "");
        if ("quit".equals(op)) {
            shutdown();
            return;
        }
        if ("refresh".equals(op)) {
            lastSentArtHash.clear();
            emitSnapshot();
            return;
        }
        String pkg = cmd.optString("package", "");
        TrackedSession session = pkg.isEmpty() ? firstSession() : trackedByPkg.get(pkg);
        if (session == null) {
            return;
        }
        MediaController c = session.controller;
        MediaController.TransportControls tc = c.getTransportControls();
        try {
            switch (op) {
                case "play":
                    tc.play();
                    break;
                case "pause":
                    tc.pause();
                    break;
                case "play_pause": {
                    PlaybackState ps = c.getPlaybackState();
                    int st = ps != null ? ps.getState() : PlaybackState.STATE_NONE;
                    if (st == PlaybackState.STATE_PLAYING || st == PlaybackState.STATE_BUFFERING) {
                        tc.pause();
                    } else {
                        tc.play();
                    }
                    break;
                }
                case "stop":
                    tc.stop();
                    break;
                case "next":
                    tc.skipToNext();
                    break;
                case "previous":
                    tc.skipToPrevious();
                    break;
                case "seek_to": {
                    long posMs = Math.max(0L, cmd.optLong("position_ms", 0L));
                    tc.seekTo(posMs);
                    break;
                }
                case "set_rate": {
                    double rate = cmd.optDouble("rate", 1.0);
                    if (rate > 0.0) {
                        tc.setPlaybackSpeed((float) rate);
                    }
                    break;
                }
                case "set_volume": {
                    double vol = Math.max(0.0, Math.min(1.0, cmd.optDouble("volume", 1.0)));
                    MediaController.PlaybackInfo pi = c.getPlaybackInfo();
                    if (pi != null
                            && pi.getPlaybackType()
                                    == MediaController.PlaybackInfo.PLAYBACK_TYPE_REMOTE
                            && pi.getMaxVolume() > 0) {
                        c.setVolumeTo((int) Math.round(vol * pi.getMaxVolume()), 0);
                    } else if (audioManager != null) {
                        int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                        audioManager.setStreamVolume(
                                AudioManager.STREAM_MUSIC, (int) Math.round(vol * max), 0);
                    }
                    lastMusicVolume = safeStreamVolume();
                    scheduleEmit();
                    break;
                }
                case "set_loop_status": {
                    String ls = cmd.optString("loop_status", "None");
                    int mode = "Track".equals(ls) ? 1 : ("Playlist".equals(ls) ? 2 : 0);
                    if (session.compatModeSupported) {
                        Bundle b = new Bundle();
                        b.putInt(COMPAT_ARG_REPEAT_MODE, mode);
                        tc.sendCustomAction(COMPAT_ACTION_SET_REPEAT_MODE, b);
                    } else {
                        invokeModeCustomAction(c, tc);
                    }
                    break;
                }
                case "set_shuffle": {
                    boolean shuffle = cmd.optBoolean("shuffle", false);
                    if (session.compatModeSupported) {
                        Bundle b = new Bundle();
                        b.putInt(COMPAT_ARG_SHUFFLE_MODE, shuffle ? 1 : 0);
                        tc.sendCustomAction(COMPAT_ACTION_SET_SHUFFLE_MODE, b);
                    } else {
                        invokeModeCustomAction(c, tc);
                    }
                    break;
                }
                case "custom_action": {
                    String actionName = cmd.optString("action", "");
                    invokeCustomActionByName(c, tc, actionName);
                    break;
                }
                default:
                    break;
            }
        } catch (Throwable ignored) {
        }
    }

    private TrackedSession firstSession() {
        for (TrackedSession s : trackedByPkg.values()) {
            return s;
        }
        return null;
    }

    private static void invokeModeCustomAction(
            MediaController c, MediaController.TransportControls tc) {
        PlaybackState ps = c.getPlaybackState();
        if (ps == null || ps.getCustomActions() == null) {
            return;
        }
        for (PlaybackState.CustomAction ca : ps.getCustomActions()) {
            if (ca == null) {
                continue;
            }
            String act = safeString(ca.getAction());
            String name = ca.getName() != null ? ca.getName().toString() : "";
            if (isPlayModeKeyword(act, name)) {
                tc.sendCustomAction(ca, ca.getExtras());
                return;
            }
        }
    }

    private static void invokeCustomActionByName(
            MediaController c, MediaController.TransportControls tc, String target) {
        if (target.isEmpty()) {
            return;
        }
        PlaybackState ps = c.getPlaybackState();
        if (ps == null || ps.getCustomActions() == null) {
            return;
        }
        for (PlaybackState.CustomAction ca : ps.getCustomActions()) {
            if (ca != null && target.equals(ca.getAction())) {
                tc.sendCustomAction(ca, ca.getExtras());
                return;
            }
        }
    }

    private static boolean isPlayModeKeyword(String action, String name) {
        String combined = (action + " " + name).toLowerCase(Locale.ROOT);
        return combined.contains("播放模式")
                || combined.contains("play_mode")
                || combined.contains("playmode")
                || combined.contains("repeat")
                || combined.contains("loop")
                || combined.contains("shuffle");
    }

    private static String mapPlaybackStatus(int state) {
        switch (state) {
            case PlaybackState.STATE_PLAYING:
            case PlaybackState.STATE_BUFFERING:
            case PlaybackState.STATE_FAST_FORWARDING:
            case PlaybackState.STATE_REWINDING:
                return "Playing";
            case PlaybackState.STATE_PAUSED:
            case PlaybackState.STATE_CONNECTING:
                return "Paused";
            default:
                return "Stopped";
        }
    }

    private static String safeString(String s) {
        return s != null ? s : "";
    }

    private static String firstNonEmpty(String... values) {
        for (String v : values) {
            if (v != null) {
                String trimmed = v.trim();
                if (!trimmed.isEmpty()) {
                    return trimmed;
                }
            }
        }
        return "";
    }

    private static String sha256Hex(byte[] bytes) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] digest = md.digest(bytes);
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }

    private static synchronized void emitLine(JSONObject obj) {
        System.out.println(obj.toString());
        System.out.flush();
    }
}
