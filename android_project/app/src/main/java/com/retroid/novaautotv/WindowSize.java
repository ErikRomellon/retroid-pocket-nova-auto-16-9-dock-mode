package com.retroid.novaautotv;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Point;
import android.os.Build;
import android.os.IBinder;
import android.view.Display;

import org.lsposed.hiddenapibypass.HiddenApiBypass;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

/**
 * Equivalente a `wm size` llamando directamente a IWindowManager desde la propia app.
 * WindowManagerService solo exige WRITE_SECURE_SETTINGS, que se concede una vez por adb.
 */
public final class WindowSize {

    private static Object windowManager;

    private WindowSize() {}

    public static boolean hasPermission(Context c) {
        return c.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED;
    }

    private static synchronized Object wm() throws Exception {
        if (windowManager == null) {
            if (Build.VERSION.SDK_INT >= 28) HiddenApiBypass.addHiddenApiExemptions("L");
            Class<?> sm = Class.forName("android.os.ServiceManager");
            IBinder binder = (IBinder) sm.getMethod("getService", String.class).invoke(null, Context.WINDOW_SERVICE);
            Class<?> stub = Class.forName("android.view.IWindowManager$Stub");
            windowManager = stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
        }
        return windowManager;
    }

    private static Object call(String name, Class<?>[] types, Object... args) throws Exception {
        Object wm = wm();
        Method m = wm.getClass().getMethod(name, types);
        try {
            return m.invoke(wm, args);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause();
            throw cause instanceof Exception ? (Exception) cause : e;
        }
    }

    /** `wm size WxH` */
    public static void set(int width, int height) throws Exception {
        call("setForcedDisplaySize", new Class<?>[]{int.class, int.class, int.class},
                Display.DEFAULT_DISPLAY, width, height);
    }

    /** `wm size WxH` a partir de un texto "WxH" */
    public static void set(String resolution) throws Exception {
        String[] p = resolution.split("x");
        set(Integer.parseInt(p[0]), Integer.parseInt(p[1]));
    }

    /** `wm size reset` */
    public static void reset() throws Exception {
        call("clearForcedDisplaySize", new Class<?>[]{int.class}, Display.DEFAULT_DISPLAY);
    }

    /** Salida al estilo de `wm size` */
    public static String query() throws Exception {
        Point initial = new Point();
        Point base = new Point();
        call("getInitialDisplaySize", new Class<?>[]{int.class, Point.class}, Display.DEFAULT_DISPLAY, initial);
        call("getBaseDisplaySize", new Class<?>[]{int.class, Point.class}, Display.DEFAULT_DISPLAY, base);
        String out = "Physical size: " + initial.x + "x" + initial.y;
        if (!initial.equals(base)) out += "\nOverride size: " + base.x + "x" + base.y;
        return out;
    }
}
