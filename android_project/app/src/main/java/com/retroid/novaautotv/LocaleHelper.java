package com.retroid.novaautotv;

import android.content.Context;
import android.content.res.Configuration;

import java.util.Locale;

/** Aplica el idioma elegido en la app sobre el del sistema. */
public final class LocaleHelper {

    /** "" = idioma del sistema. Mismo orden que NAMES. */
    public static final String[] CODES = {"", "en", "es", "pt", "fr"};
    /** El primero se sustituye por R.string.language_system. */
    public static final String[] NAMES = {"", "English", "Español", "Português", "Français"};

    private LocaleHelper() {}

    public static Context wrap(Context base) {
        String lang = Prefs.getLanguage(base);
        if (lang.isEmpty()) return base;
        Configuration cfg = new Configuration(base.getResources().getConfiguration());
        cfg.setLocale(Locale.forLanguageTag(lang));
        return base.createConfigurationContext(cfg);
    }

    public static int indexOf(String lang) {
        for (int i = 0; i < CODES.length; i++) {
            if (CODES[i].equals(lang)) return i;
        }
        return 0;
    }
}
