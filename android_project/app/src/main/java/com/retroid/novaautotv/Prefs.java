package com.retroid.novaautotv;

import android.content.Context;
import android.content.SharedPreferences;

public final class Prefs {
    public static final String DEFAULT_RESOLUTION = "1080x1920";
    public static final int DEFAULT_DELAY_MS = 2000;

    private static final String NAME = "settings";
    private static final String KEY_ENABLED = "enabled";
    private static final String KEY_RESOLUTION = "resolution";
    private static final String KEY_DELAY = "delay_ms";
    private static final String KEY_LANGUAGE = "language";

    private Prefs() {}

    private static SharedPreferences sp(Context c) {
        return c.getSharedPreferences(NAME, Context.MODE_PRIVATE);
    }

    public static boolean isEnabled(Context c) {
        return sp(c).getBoolean(KEY_ENABLED, false);
    }

    public static void setEnabled(Context c, boolean v) {
        sp(c).edit().putBoolean(KEY_ENABLED, v).apply();
    }

    public static String getResolution(Context c) {
        return sp(c).getString(KEY_RESOLUTION, DEFAULT_RESOLUTION);
    }

    public static int getDelayMs(Context c) {
        return sp(c).getInt(KEY_DELAY, DEFAULT_DELAY_MS);
    }

    public static void save(Context c, String resolution, int delayMs) {
        sp(c).edit().putString(KEY_RESOLUTION, resolution).putInt(KEY_DELAY, delayMs).apply();
    }

    /** Codigo de idioma elegido ("" = el del sistema). */
    public static String getLanguage(Context c) {
        return sp(c).getString(KEY_LANGUAGE, "");
    }

    public static void setLanguage(Context c, String lang) {
        sp(c).edit().putString(KEY_LANGUAGE, lang).apply();
    }

    public static boolean isValidResolution(String s) {
        return s != null && s.matches("\\d{2,5}x\\d{2,5}");
    }
}
