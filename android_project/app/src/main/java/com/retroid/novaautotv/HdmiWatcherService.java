package com.retroid.novaautotv;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.hardware.display.DisplayManager;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

public class HdmiWatcherService extends Service {

    public static final String ACTION_REFRESH = "com.retroid.novaautotv.REFRESH";
    public static final String ACTION_LOCALE = "com.retroid.novaautotv.LOCALE";
    private static final String TAG = "NovaAutoTV";
    private static final String CHANNEL = "watcher";
    private static final int NOTIF_ID = 1;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean audioPlugged;
    /** Estado que se quiere aplicar (null = aun no evaluado). */
    private Boolean desired;
    /** Ultimo estado aplicado con exito (null = desconocido). */
    private Boolean applied;
    /** Contexto con el idioma elegido en la app; se rehace al cambiarlo. */
    private Context tr;
    private int statusRes = R.string.status_starting;
    private Object[] statusArgs = {};

    private final BroadcastReceiver hdmiReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            audioPlugged = intent.getIntExtra(AudioManager.EXTRA_AUDIO_PLUG_STATE, 0) == 1;
            Log.i(TAG, "HDMI audio plug = " + audioPlugged);
            evaluate(false);
        }
    };

    private final DisplayManager.DisplayListener displayListener = new DisplayManager.DisplayListener() {
        @Override public void onDisplayAdded(int displayId) { evaluate(false); }
        @Override public void onDisplayRemoved(int displayId) { evaluate(false); }
        @Override public void onDisplayChanged(int displayId) {}
    };

    private final Runnable applyRunnable = this::apply;

    @Override
    public void onCreate() {
        super.onCreate();
        tr = LocaleHelper.wrap(this);
        createChannel();
        Notification n = buildNotification();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIF_ID, n);
        }

        IntentFilter f = new IntentFilter(AudioManager.ACTION_HDMI_AUDIO_PLUG);
        Intent sticky;
        if (Build.VERSION.SDK_INT >= 33) {
            sticky = registerReceiver(hdmiReceiver, f, Context.RECEIVER_EXPORTED);
        } else {
            sticky = registerReceiver(hdmiReceiver, f);
        }
        if (sticky != null) {
            audioPlugged = sticky.getIntExtra(AudioManager.EXTRA_AUDIO_PLUG_STATE, 0) == 1;
        }
        getSystemService(DisplayManager.class).registerDisplayListener(displayListener, handler);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : null;
        if (ACTION_LOCALE.equals(action)) {
            tr = LocaleHelper.wrap(this);
            createChannel();
            updateNotification();
        }
        boolean force = ACTION_REFRESH.equals(action);
        if (force) applied = null;
        evaluate(force);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(applyRunnable);
        try { unregisterReceiver(hdmiReceiver); } catch (Exception ignored) {}
        getSystemService(DisplayManager.class).unregisterDisplayListener(displayListener);
        // Si se apaga el servicio con 16:9 aplicado, dejar la pantalla como estaba.
        if (Boolean.TRUE.equals(applied)) {
            try { WindowSize.reset(); } catch (Exception e) { Log.w(TAG, "reset", e); }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private boolean isConnectedNow() {
        return audioPlugged || HdmiState.hasPresentationDisplay(this);
    }

    private void evaluate(boolean force) {
        boolean connected = isConnectedNow();
        if (!force && desired != null && desired == connected) return;
        desired = connected;
        handler.removeCallbacks(applyRunnable);
        int delay = Prefs.getDelayMs(this);
        setStatus(connected ? R.string.status_pending_on : R.string.status_pending_off, delay);
        handler.postDelayed(applyRunnable, delay);
    }

    private void apply() {
        if (desired == null || desired.equals(applied)) return;
        if (!WindowSize.hasPermission(this)) {
            setStatus(R.string.status_no_perm);
            return;
        }
        boolean target = desired;
        String res = Prefs.getResolution(this);
        try {
            if (target) WindowSize.set(res);
            else WindowSize.reset();
            applied = target;
            Log.i(TAG, target ? "wm size " + res : "wm size reset");
            if (target) setStatus(R.string.status_169, res);
            else setStatus(R.string.status_43);
        } catch (Exception e) {
            Log.e(TAG, "apply", e);
            setStatus(R.string.status_error, e.toString());
        }
    }

    private void setStatus(int res, Object... args) {
        statusRes = res;
        statusArgs = args;
        updateNotification();
    }

    private void createChannel() {
        getSystemService(NotificationManager.class).createNotificationChannel(
                new NotificationChannel(CHANNEL, tr.getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW));
    }

    private void updateNotification() {
        getSystemService(NotificationManager.class).notify(NOTIF_ID, buildNotification());
    }

    private Notification buildNotification() {
        PendingIntent pi = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_tv)
                .setContentTitle(tr.getString(R.string.app_name))
                .setContentText(tr.getString(statusRes, statusArgs))
                .setSubText(tr.getString(R.string.credit))
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    public static void start(Context c, boolean refresh) {
        Intent i = new Intent(c, HdmiWatcherService.class);
        if (refresh) i.setAction(ACTION_REFRESH);
        c.startForegroundService(i);
    }

    /** Rehace los textos de la notificacion tras cambiar el idioma de la app. */
    public static void relocalize(Context c) {
        c.startForegroundService(new Intent(c, HdmiWatcherService.class).setAction(ACTION_LOCALE));
    }

    public static void stop(Context c) {
        c.stopService(new Intent(c, HdmiWatcherService.class));
    }
}
