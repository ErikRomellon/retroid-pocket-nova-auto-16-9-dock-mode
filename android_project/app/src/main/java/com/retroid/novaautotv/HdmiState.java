package com.retroid.novaautotv;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.display.DisplayManager;
import android.media.AudioManager;

public final class HdmiState {
    private HdmiState() {}

    public static boolean hasPresentationDisplay(Context c) {
        DisplayManager dm = (DisplayManager) c.getSystemService(Context.DISPLAY_SERVICE);
        return dm != null && dm.getDisplays(DisplayManager.DISPLAY_CATEGORY_PRESENTATION).length > 0;
    }

    /** Lee el broadcast sticky de HDMI sin registrar un receptor. */
    public static boolean isAudioPlugged(Context c) {
        Intent i = c.registerReceiver(null, new IntentFilter(AudioManager.ACTION_HDMI_AUDIO_PLUG));
        return i != null && i.getIntExtra(AudioManager.EXTRA_AUDIO_PLUG_STATE, 0) == 1;
    }

    public static boolean isConnected(Context c) {
        return isAudioPlugged(c) || hasPresentationDisplay(c);
    }
}
