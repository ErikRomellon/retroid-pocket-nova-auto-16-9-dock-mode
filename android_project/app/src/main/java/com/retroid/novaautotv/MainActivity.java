package com.retroid.novaautotv;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Spinner;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {

    private TextView permStatus;
    private TextView grantCommand;
    private Button copyButton;
    private TextView hdmiStatus;
    private TextView output;
    private Switch serviceSwitch;
    private EditText resolution;
    private EditText delay;

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(LocaleHelper.wrap(base));
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        permStatus = findViewById(R.id.perm_status);
        grantCommand = findViewById(R.id.grant_command);
        copyButton = findViewById(R.id.copy_button);
        hdmiStatus = findViewById(R.id.hdmi_status);
        output = findViewById(R.id.output);
        serviceSwitch = findViewById(R.id.service_switch);
        resolution = findViewById(R.id.resolution);
        delay = findViewById(R.id.delay);

        grantCommand.setText(grantCmd());
        resolution.setText(Prefs.getResolution(this));
        delay.setText(String.valueOf(Prefs.getDelayMs(this)));
        serviceSwitch.setChecked(Prefs.isEnabled(this));
        setupLanguage();

        copyButton.setOnClickListener(v -> {
            getSystemService(ClipboardManager.class).setPrimaryClip(ClipData.newPlainText("adb", grantCmd()));
            Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show();
        });
        serviceSwitch.setOnCheckedChangeListener((b, checked) -> {
            Prefs.setEnabled(this, checked);
            if (checked) HdmiWatcherService.start(this, true);
            else HdmiWatcherService.stop(this);
        });
        findViewById(R.id.save_button).setOnClickListener(v -> save());
        findViewById(R.id.apply_button).setOnClickListener(v -> {
            if (save()) runAction("wm size " + Prefs.getResolution(this), () -> {
                WindowSize.set(Prefs.getResolution(this));
                return WindowSize.query();
            });
        });
        findViewById(R.id.reset_button).setOnClickListener(v -> runAction("wm size reset", () -> {
            WindowSize.reset();
            return WindowSize.query();
        }));
        findViewById(R.id.query_button).setOnClickListener(v -> runAction("wm size", WindowSize::query));

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        if (Prefs.isEnabled(this)) HdmiWatcherService.start(this, false);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    private void setupLanguage() {
        String[] names = LocaleHelper.NAMES.clone();
        names[0] = getString(R.string.language_system);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, names);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        Spinner language = findViewById(R.id.language);
        language.setAdapter(adapter);
        language.setSelection(LocaleHelper.indexOf(Prefs.getLanguage(this)), false);
        language.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                String lang = LocaleHelper.CODES[position];
                if (lang.equals(Prefs.getLanguage(MainActivity.this))) return;
                Prefs.setLanguage(MainActivity.this, lang);
                if (Prefs.isEnabled(MainActivity.this)) HdmiWatcherService.relocalize(MainActivity.this);
                recreate();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {}
        });
    }

    private String grantCmd() {
        return "adb shell pm grant " + getPackageName() + " android.permission.WRITE_SECURE_SETTINGS";
    }

    private void refresh() {
        boolean granted = WindowSize.hasPermission(this);
        permStatus.setText(granted ? R.string.perm_ok : R.string.perm_missing);
        int vis = granted ? View.GONE : View.VISIBLE;
        grantCommand.setVisibility(vis);
        copyButton.setVisibility(vis);

        boolean audio = HdmiState.isAudioPlugged(this);
        boolean pres = HdmiState.hasPresentationDisplay(this);
        hdmiStatus.setText(getString(R.string.hdmi_status,
                (audio || pres) ? getString(R.string.connected) : getString(R.string.disconnected),
                getString(audio ? R.string.yes : R.string.no), getString(pres ? R.string.yes : R.string.no)));
    }

    private boolean save() {
        String res = resolution.getText().toString().trim();
        if (!Prefs.isValidResolution(res)) {
            resolution.setError(getString(R.string.invalid_resolution));
            return false;
        }
        int ms;
        try {
            ms = Integer.parseInt(delay.getText().toString().trim());
            if (ms < 0 || ms > 60000) throw new NumberFormatException();
        } catch (NumberFormatException e) {
            delay.setError(getString(R.string.invalid_delay));
            return false;
        }
        Prefs.save(this, res, ms);
        Toast.makeText(this, R.string.saved, Toast.LENGTH_SHORT).show();
        if (Prefs.isEnabled(this)) HdmiWatcherService.start(this, true);
        return true;
    }

    private interface Action {
        String run() throws Exception;
    }

    private void runAction(String label, Action action) {
        String result;
        try {
            result = action.run();
        } catch (SecurityException e) {
            result = getString(R.string.perm_missing) + "\n" + e.getMessage();
        } catch (Exception e) {
            result = getString(R.string.status_error, e.toString());
        }
        output.setText("$ " + label + "\n" + result);
    }
}
