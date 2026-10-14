#!/usr/bin/env python3
"""
Genera y compila la APK "Nova Auto TV" para Retroid Pocket Nova, y los
instaladores de Windows (NovaAutoTV-Install.exe / NovaAutoTV-Uninstall.exe).

La APK cambia automaticamente la resolucion a 16:9 (`wm size 1080x1920`) al
conectar HDMI y la restaura (`wm size reset`) al desconectar. Sin root y sin
apps externas: la app llama a IWindowManager directamente con el permiso
WRITE_SECURE_SETTINGS, que se concede una sola vez por adb (--install lo hace).

Creada por ElPim_.

Uso:
    python3 retroid_pocket_nova_android_automatic16-9_automatic_tv.py
        [--install] [--grant] [--debug] [--clean] [--no-exe] [--uninstall [--yes]]

Solo usa la libreria estandar. Lo que falte se descarga en ./.toolchain/:
JDK 17 (si no hay uno en el PATH), Android SDK, Gradle, adb y, para los .exe,
Python para Windows + PyInstaller dentro de un prefijo de wine (wine debe
estar instalado en el sistema).
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLCHAIN = ROOT / ".toolchain"
DOWNLOADS = TOOLCHAIN / "downloads"
JDK_DIR = TOOLCHAIN / "jdk"
SDK_DIR = TOOLCHAIN / "android-sdk"
SDKMANAGER = SDK_DIR / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
GRADLE_VERSION = "8.7"
GRADLE_DIR = TOOLCHAIN / f"gradle-{GRADLE_VERSION}"
GRADLE_HOME = TOOLCHAIN / "gradle-home"
KEYSTORE = TOOLCHAIN / "release.jks"
WINE_PREFIX = TOOLCHAIN / "wine"
PROJECT = ROOT / "android_project"
BUILD_EXE = ROOT / "build_exe"
DIST = ROOT / "dist"
APK_OUT = DIST / "NovaAutoTV.apk"
EXTRA_IMAGE = ROOT / "static" / "files" / "resources" / "images" / "grain.png"

CMDLINE_TOOLS_URL = "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
GRADLE_URL = f"https://services.gradle.org/distributions/gradle-{GRADLE_VERSION}-bin.zip"
JDK_URL = "https://api.adoptium.net/v3/binary/latest/17/ga/linux/{arch}/jdk/hotspot/normal/eclipse"
SDK_PACKAGES = ["platforms;android-34", "build-tools;34.0.0"]

PY_WIN_VERSION = "3.12.10"
PY_WIN_URL = f"https://www.python.org/ftp/python/{PY_WIN_VERSION}/python-{PY_WIN_VERSION}-amd64.exe"
PY_WIN_DIR = WINE_PREFIX / "drive_c" / "Python312"
PY_WIN_EXE = r"C:\Python312\python.exe"

KEY_ALIAS = "novaautotv"
KEY_PASS = "novaautotv"

PACKAGE = "com.retroid.novaautotv"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def log(msg):
    print(f"\033[1;36m==>\033[0m {msg}", flush=True)


def warn(msg):
    print(f"\033[1;33mAVISO:\033[0m {msg}", flush=True)


def die(msg):
    print(f"\033[1;31mERROR:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, check=True, **kw):
    print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=check, **kw)


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"Descargando {url}")
    # api.adoptium.net rechaza (403) el User-Agent por defecto "Python-urllib".
    req = urllib.request.Request(url, headers={"User-Agent": "NovaAutoTV-build"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r   {done * 100 // total:3d}% ({done >> 20} / {total >> 20} MiB)", end="", flush=True)
    print()
    tmp.rename(dest)


def extract_zip(zip_path, dest):
    """Extrae conservando los permisos de ejecucion (zipfile no lo hace solo)."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest not in target.parents and target != dest:
                die(f"Ruta sospechosa en {zip_path}: {info.filename}")
            zf.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode)


# --------------------------------------------------------------------------
# Toolchain
# --------------------------------------------------------------------------

_java_ready = False


def java_major(java):
    out = subprocess.run([java, "-version"], capture_output=True, text=True)
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return 0
    major = int(m.group(1))
    if major == 1:
        major = int(m.group(2) or 0)
    return major


def ensure_java():
    """Usa el JDK 17+ del PATH o, si no hay, descarga Temurin 17 en .toolchain/jdk/.

    El JDK descargado se exporta en JAVA_HOME y PATH de este proceso, asi que gradle,
    keytool y sdkmanager lo heredan.
    """
    global _java_ready
    if _java_ready:
        return
    java = shutil.which("java")
    major = java_major(java) if java else 0
    if major >= 17 and shutil.which("keytool"):
        log(f"Java {major} OK")
    else:
        if java:
            log(f"Java {major} es demasiado antiguo; se usara un JDK 17 propio")
        if not (JDK_DIR / "bin" / "java").exists():
            arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
                platform.machine().lower())
            if not arch:
                die(f"Arquitectura no soportada para descargar el JDK: {platform.machine()}")
            tgz = DOWNLOADS / f"temurin-17-{arch}.tar.gz"
            if not tgz.exists():
                download(JDK_URL.format(arch=arch), tgz)
            tmp = TOOLCHAIN / "jdk-tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            with tarfile.open(tgz) as tf:
                tf.extractall(tmp, filter="data")
            tops = [d for d in tmp.iterdir() if d.is_dir()]
            if len(tops) != 1:
                die(f"Contenido inesperado en {tgz}")
            shutil.rmtree(JDK_DIR, ignore_errors=True)
            shutil.move(str(tops[0]), str(JDK_DIR))
            shutil.rmtree(tmp, ignore_errors=True)
        os.environ["JAVA_HOME"] = str(JDK_DIR)
        os.environ["PATH"] = str(JDK_DIR / "bin") + os.pathsep + os.environ.get("PATH", "")
        log(f"Java {java_major(str(JDK_DIR / 'bin' / 'java'))} (Temurin en {JDK_DIR}) OK")
    _java_ready = True


def sdk_install(packages):
    log("Aceptando licencias del Android SDK")
    run([SDKMANAGER, f"--sdk_root={SDK_DIR}", "--licenses"],
        input="y\n" * 50, text=True, stdout=subprocess.DEVNULL)
    log("Instalando " + ", ".join(packages))
    run([SDKMANAGER, f"--sdk_root={SDK_DIR}", *packages])


def ensure_sdk():
    if not SDKMANAGER.exists():
        zip_path = DOWNLOADS / "cmdline-tools.zip"
        if not zip_path.exists():
            download(CMDLINE_TOOLS_URL, zip_path)
        tmp = TOOLCHAIN / "cmdline-tools-tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        extract_zip(zip_path, tmp)
        latest = SDKMANAGER.parent.parent
        latest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(latest, ignore_errors=True)
        shutil.move(str(tmp / "cmdline-tools"), str(latest))
        shutil.rmtree(tmp, ignore_errors=True)

    missing = [p for p in SDK_PACKAGES if not (SDK_DIR / p.replace(";", "/")).exists()]
    if missing:
        sdk_install(missing)
    log("Android SDK OK")


def ensure_gradle():
    gradle = GRADLE_DIR / "bin" / "gradle"
    if not gradle.exists():
        zip_path = DOWNLOADS / f"gradle-{GRADLE_VERSION}-bin.zip"
        if not zip_path.exists():
            download(GRADLE_URL, zip_path)
        extract_zip(zip_path, TOOLCHAIN)
    log(f"Gradle {GRADLE_VERSION} OK")
    return gradle


def ensure_keystore():
    if KEYSTORE.exists():
        return
    log("Generando keystore de firma (una sola vez)")
    run(["keytool", "-genkeypair", "-keystore", KEYSTORE, "-storepass", KEY_PASS,
         "-keypass", KEY_PASS, "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
         "-validity", "10000", "-dname", "CN=NovaAutoTV, O=Retroid Nova, C=ES"])


def wine_env():
    return dict(os.environ, WINEPREFIX=str(WINE_PREFIX), WINEARCH="win64", WINEDEBUG="-all",
                # Evita los dialogos de instalar Mono/Gecko al crear el prefijo.
                WINEDLLOVERRIDES="mscoree,mshtml=")


def ensure_wine_python():
    """Python para Windows + PyInstaller en un prefijo de wine propio. None si no hay wine."""
    if not shutil.which("wine"):
        warn("No se encontro 'wine': se omiten los .exe de Windows. Instalalo con el gestor de paquetes\n"
             "       (Arch: sudo pacman -S wine | Debian/Ubuntu: sudo apt install wine | Fedora: sudo dnf install wine)\n"
             "       y vuelve a ejecutar el script.")
        return None
    env = wine_env()
    if not (PY_WIN_DIR / "python.exe").exists():
        installer = DOWNLOADS / f"python-{PY_WIN_VERSION}-amd64.exe"
        if not installer.exists():
            download(PY_WIN_URL, installer)
        log(f"Instalando Python {PY_WIN_VERSION} para Windows en {WINE_PREFIX}")
        run(["wine", installer, "/quiet", "InstallAllUsers=0", r"TargetDir=C:\Python312",
             "Include_tcltk=1", "Include_pip=1", "Include_test=0", "Include_doc=0",
             "Include_launcher=0", "Shortcuts=0", "AssociateFiles=0"], env=env)
        if not (PY_WIN_DIR / "python.exe").exists():
            die(f"La instalacion de Python en wine fallo ({PY_WIN_DIR} no existe).")
    if not (PY_WIN_DIR / "Lib" / "site-packages" / "PyInstaller").exists():
        log("Instalando PyInstaller en el Python de wine")
        run(["wine", PY_WIN_EXE, "-m", "pip", "install", "--disable-pip-version-check", "pyinstaller"], env=env)
    log("Python para Windows + PyInstaller OK")
    return env


# --------------------------------------------------------------------------
# Plantillas del proyecto Android
# --------------------------------------------------------------------------

SETTINGS_GRADLE = r'''pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "NovaAutoTV"
include ':app'
'''

ROOT_BUILD_GRADLE = r'''plugins {
    id 'com.android.application' version '8.5.2' apply false
}
'''

GRADLE_PROPERTIES = r'''org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
android.useAndroidX=true
android.nonTransitiveRClass=true
'''

APP_BUILD_GRADLE = r'''plugins {
    id 'com.android.application'
}

android {
    namespace '@@PACKAGE@@'
    compileSdk 34

    defaultConfig {
        applicationId '@@PACKAGE@@'
        minSdk 26
        targetSdk 34
        versionCode 3
        versionName '3.0'
    }

    signingConfigs {
        release {
            storeFile file('@@KEYSTORE@@')
            storePassword '@@KEY_PASS@@'
            keyAlias '@@KEY_ALIAS@@'
            keyPassword '@@KEY_PASS@@'
        }
    }

    buildTypes {
        release {
            // Sin minify: WindowSize usa reflexion sobre APIs ocultas.
            minifyEnabled false
            signingConfig signingConfigs.release
        }
    }

    buildFeatures {
        buildConfig true
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_1_8
        targetCompatibility JavaVersion.VERSION_1_8
    }

    lint {
        checkReleaseBuilds false
        abortOnError false
    }
}

dependencies {
    // Libreria (no app externa) para poder llamar a APIs ocultas en Android 9+.
    implementation 'org.lsposed.hiddenapibypass:hiddenapibypass:4.3'
}
'''

MANIFEST = r'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE" />
    <uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
    <!-- Se concede una vez: adb shell pm grant @@PACKAGE@@ android.permission.WRITE_SECURE_SETTINGS -->
    <uses-permission android:name="android.permission.WRITE_SECURE_SETTINGS" />

    <application
        android:allowBackup="true"
        android:icon="@drawable/ic_tv"
        android:label="@string/app_name"
        android:theme="@android:style/Theme.DeviceDefault">

        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>

        <service
            android:name=".HdmiWatcherService"
            android:exported="false"
            android:foregroundServiceType="specialUse">
            <property
                android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="Detecta conexion HDMI y ajusta la resolucion de pantalla" />
        </service>

        <receiver
            android:name=".BootReceiver"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
                <action android:name="android.intent.action.MY_PACKAGE_REPLACED" />
            </intent-filter>
        </receiver>
    </application>
</manifest>
'''

JAVA_PREFS = r'''package @@PACKAGE@@;

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
'''

JAVA_LOCALE_HELPER = r'''package @@PACKAGE@@;

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
'''

JAVA_WINDOW_SIZE = r'''package @@PACKAGE@@;

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
'''

JAVA_HDMI_STATE = r'''package @@PACKAGE@@;

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
'''

JAVA_SERVICE = r'''package @@PACKAGE@@;

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

    public static final String ACTION_REFRESH = "@@PACKAGE@@.REFRESH";
    public static final String ACTION_LOCALE = "@@PACKAGE@@.LOCALE";
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
'''

JAVA_BOOT = r'''package @@PACKAGE@@;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (Prefs.isEnabled(context)) {
            HdmiWatcherService.start(context, false);
        }
    }
}
'''

JAVA_MAIN = r'''package @@PACKAGE@@;

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
'''

LAYOUT_MAIN = r'''<?xml version="1.0" encoding="utf-8"?>
<ScrollView xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent">

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="vertical"
        android:padding="16dp">

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="@string/app_name"
            android:textSize="24sp"
            android:textStyle="bold" />

        <LinearLayout
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="8dp"
            android:gravity="center_vertical"
            android:orientation="horizontal">

            <TextView
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:text="@string/language_label" />

            <Spinner
                android:id="@+id/language"
                android:layout_width="0dp"
                android:layout_height="wrap_content"
                android:layout_weight="1" />
        </LinearLayout>

        <TextView
            android:id="@+id/perm_status"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="16dp"
            android:textSize="16sp" />

        <TextView
            android:id="@+id/grant_command"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:fontFamily="monospace"
            android:textIsSelectable="true"
            android:visibility="gone" />

        <Button
            android:id="@+id/copy_button"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="@string/copy_command"
            android:visibility="gone" />

        <TextView
            android:id="@+id/hdmi_status"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="8dp"
            android:textSize="16sp" />

        <Switch
            android:id="@+id/service_switch"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="16dp"
            android:text="@string/service_active"
            android:textSize="18sp" />

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_marginTop="16dp"
            android:text="@string/resolution_label" />

        <EditText
            android:id="@+id/resolution"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:inputType="text"
            android:singleLine="true" />

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="@string/delay_label" />

        <EditText
            android:id="@+id/delay"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:inputType="number"
            android:singleLine="true" />

        <Button
            android:id="@+id/save_button"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:text="@string/save" />

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_marginTop="16dp"
            android:text="@string/tests"
            android:textStyle="bold" />

        <LinearLayout
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:orientation="horizontal">

            <Button
                android:id="@+id/apply_button"
                android:layout_width="0dp"
                android:layout_height="wrap_content"
                android:layout_weight="1"
                android:text="@string/apply_169" />

            <Button
                android:id="@+id/reset_button"
                android:layout_width="0dp"
                android:layout_height="wrap_content"
                android:layout_weight="1"
                android:text="@string/reset_43" />

            <Button
                android:id="@+id/query_button"
                android:layout_width="0dp"
                android:layout_height="wrap_content"
                android:layout_weight="1"
                android:text="@string/query" />
        </LinearLayout>

        <TextView
            android:id="@+id/output"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="8dp"
            android:fontFamily="monospace"
            android:textIsSelectable="true" />

        <TextView
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="24dp"
            android:alpha="0.6"
            android:gravity="center"
            android:text="@string/credit"
            android:textSize="12sp" />
    </LinearLayout>
</ScrollView>
'''

# Textos de la app. "en" es el idioma por defecto (res/values/); todos van ademas en res/values-<idioma>/.
STRINGS = {
    "es": {
        "perm_ok": "Permiso para cambiar la resolución: concedido ✔",
        "perm_missing": "Falta el permiso WRITE_SECURE_SETTINGS. Vuelve a ejecutar NovaAutoTV-Install.exe en el PC, "
                        "o ejecuta una vez (con la Nova conectada por USB y la depuración USB activada):",
        "copy_command": "Copiar comando",
        "copied": "Comando copiado",
        "hdmi_status": "HDMI: %1$s (audio HDMI: %2$s, pantalla externa: %3$s)",
        "connected": "CONECTADO",
        "disconnected": "desconectado",
        "yes": "sí",
        "no": "no",
        "service_active": "Servicio activo",
        "resolution_label": "Resolución al conectar la TV (ancho x alto):",
        "delay_label": "Retraso antes de aplicar (ms):",
        "save": "Guardar",
        "saved": "Guardado",
        "tests": "Pruebas manuales",
        "apply_169": "Aplicar 16:9",
        "reset_43": "Restaurar 4:3",
        "query": "Ver wm size",
        "invalid_resolution": "Formato: 1080x1920",
        "invalid_delay": "Valor entre 0 y 60000",
        "language_label": "Idioma:",
        "language_system": "Sistema",
        "credit": "Creada por ElPim_",
        "channel_name": "Vigilancia HDMI",
        "status_starting": "Iniciando…",
        "status_pending_on": "TV conectada: aplicando en %1$d ms",
        "status_pending_off": "TV desconectada: aplicando en %1$d ms",
        "status_no_perm": "Falta el permiso WRITE_SECURE_SETTINGS (abre la app)",
        "status_169": "16:9 activo (%1$s)",
        "status_43": "4:3 original",
        "status_error": "Error: %1$s",
    },
    "en": {
        "perm_ok": "Permission to change the resolution: granted ✔",
        "perm_missing": "WRITE_SECURE_SETTINGS permission is missing. Run NovaAutoTV-Install.exe again on your PC, "
                        "or run this once (with the Nova connected over USB and USB debugging enabled):",
        "copy_command": "Copy command",
        "copied": "Command copied",
        "hdmi_status": "HDMI: %1$s (HDMI audio: %2$s, external display: %3$s)",
        "connected": "CONNECTED",
        "disconnected": "disconnected",
        "yes": "yes",
        "no": "no",
        "service_active": "Service active",
        "resolution_label": "Resolution when the TV is connected (width x height):",
        "delay_label": "Delay before applying (ms):",
        "save": "Save",
        "saved": "Saved",
        "tests": "Manual tests",
        "apply_169": "Apply 16:9",
        "reset_43": "Restore 4:3",
        "query": "Show wm size",
        "invalid_resolution": "Format: 1080x1920",
        "invalid_delay": "Value between 0 and 60000",
        "language_label": "Language:",
        "language_system": "System",
        "credit": "Created by ElPim_",
        "channel_name": "HDMI monitor",
        "status_starting": "Starting…",
        "status_pending_on": "TV connected: applying in %1$d ms",
        "status_pending_off": "TV disconnected: applying in %1$d ms",
        "status_no_perm": "WRITE_SECURE_SETTINGS permission is missing (open the app)",
        "status_169": "16:9 active (%1$s)",
        "status_43": "Original 4:3",
        "status_error": "Error: %1$s",
    },
    "pt": {
        "perm_ok": "Permissão para alterar a resolução: concedida ✔",
        "perm_missing": "Falta a permissão WRITE_SECURE_SETTINGS. Execute de novo o NovaAutoTV-Install.exe no PC, "
                        "ou execute uma vez (com o Nova conectado por USB e a depuração USB ativada):",
        "copy_command": "Copiar comando",
        "copied": "Comando copiado",
        "hdmi_status": "HDMI: %1$s (áudio HDMI: %2$s, tela externa: %3$s)",
        "connected": "CONECTADO",
        "disconnected": "desconectado",
        "yes": "sim",
        "no": "não",
        "service_active": "Serviço ativo",
        "resolution_label": "Resolução ao conectar a TV (largura x altura):",
        "delay_label": "Atraso antes de aplicar (ms):",
        "save": "Salvar",
        "saved": "Salvo",
        "tests": "Testes manuais",
        "apply_169": "Aplicar 16:9",
        "reset_43": "Restaurar 4:3",
        "query": "Ver wm size",
        "invalid_resolution": "Formato: 1080x1920",
        "invalid_delay": "Valor entre 0 e 60000",
        "language_label": "Idioma:",
        "language_system": "Sistema",
        "credit": "Criado por ElPim_",
        "channel_name": "Monitoramento HDMI",
        "status_starting": "Iniciando…",
        "status_pending_on": "TV conectada: aplicando em %1$d ms",
        "status_pending_off": "TV desconectada: aplicando em %1$d ms",
        "status_no_perm": "Falta a permissão WRITE_SECURE_SETTINGS (abra o app)",
        "status_169": "16:9 ativo (%1$s)",
        "status_43": "4:3 original",
        "status_error": "Erro: %1$s",
    },
    "fr": {
        "perm_ok": "Autorisation de changer la résolution : accordée ✔",
        "perm_missing": "L'autorisation WRITE_SECURE_SETTINGS manque. Relancez NovaAutoTV-Install.exe sur le PC, "
                        "ou exécutez une fois (Nova branchée en USB, débogage USB activé) :",
        "copy_command": "Copier la commande",
        "copied": "Commande copiée",
        "hdmi_status": "HDMI : %1$s (audio HDMI : %2$s, écran externe : %3$s)",
        "connected": "CONNECTÉ",
        "disconnected": "déconnecté",
        "yes": "oui",
        "no": "non",
        "service_active": "Service actif",
        "resolution_label": "Résolution quand la TV est branchée (largeur x hauteur) :",
        "delay_label": "Délai avant application (ms) :",
        "save": "Enregistrer",
        "saved": "Enregistré",
        "tests": "Tests manuels",
        "apply_169": "Appliquer 16:9",
        "reset_43": "Restaurer 4:3",
        "query": "Voir wm size",
        "invalid_resolution": "Format : 1080x1920",
        "invalid_delay": "Valeur entre 0 et 60000",
        "language_label": "Langue :",
        "language_system": "Système",
        "credit": "Créée par ElPim_",
        "channel_name": "Surveillance HDMI",
        "status_starting": "Démarrage…",
        "status_pending_on": "TV branchée : application dans %1$d ms",
        "status_pending_off": "TV débranchée : application dans %1$d ms",
        "status_no_perm": "L'autorisation WRITE_SECURE_SETTINGS manque (ouvrez l'app)",
        "status_169": "16:9 actif (%1$s)",
        "status_43": "4:3 d'origine",
        "status_error": "Erreur : %1$s",
    },
}


def android_escape(text):
    return (text.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;")
            .replace("'", "\\'").replace('"', '\\"'))


def strings_xml(lang, default=False):
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>"]
    if default:
        lines.append('    <string name="app_name" translatable="false">Nova Auto TV</string>')
    for key, text in STRINGS[lang].items():
        lines.append(f'    <string name="{key}">{android_escape(text)}</string>')
    lines.append("</resources>")
    return "\n".join(lines) + "\n"


DRAWABLE_TV = r'''<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp"
    android:height="24dp"
    android:viewportWidth="24"
    android:viewportHeight="24">
    <path
        android:fillColor="#FFFFFFFF"
        android:pathData="M21,3H3C1.9,3 1,3.9 1,5v12c0,1.1 0.9,2 2,2h5v2h8v-2h5c1.1,0 1.99,-0.9 1.99,-2L23,5c0,-1.1 -0.9,-2 -2,-2zM21,17H3V5h18v12z" />
</vector>
'''


def generate_project():
    log(f"Generando proyecto en {PROJECT}")
    # Se regenera entero: asi no quedan fuentes o idiomas que ya no existen en el script.
    shutil.rmtree(PROJECT / "app" / "src", ignore_errors=True)
    pkg_path = PACKAGE.replace(".", "/")
    main = "app/src/main"
    files = {
        "settings.gradle": SETTINGS_GRADLE,
        "build.gradle": ROOT_BUILD_GRADLE,
        "gradle.properties": GRADLE_PROPERTIES,
        "local.properties": f"sdk.dir={SDK_DIR}\n",
        "app/build.gradle": APP_BUILD_GRADLE,
        f"{main}/AndroidManifest.xml": MANIFEST,
        f"{main}/java/{pkg_path}/Prefs.java": JAVA_PREFS,
        f"{main}/java/{pkg_path}/LocaleHelper.java": JAVA_LOCALE_HELPER,
        f"{main}/java/{pkg_path}/WindowSize.java": JAVA_WINDOW_SIZE,
        f"{main}/java/{pkg_path}/HdmiState.java": JAVA_HDMI_STATE,
        f"{main}/java/{pkg_path}/HdmiWatcherService.java": JAVA_SERVICE,
        f"{main}/java/{pkg_path}/BootReceiver.java": JAVA_BOOT,
        f"{main}/java/{pkg_path}/MainActivity.java": JAVA_MAIN,
        f"{main}/res/layout/activity_main.xml": LAYOUT_MAIN,
        f"{main}/res/drawable/ic_tv.xml": DRAWABLE_TV,
    }
    # El ingles va como idioma por defecto (para sistemas en otros idiomas) y ademas en values-en,
    # para que la APK lo anuncie.
    files[f"{main}/res/values/strings.xml"] = strings_xml("en", default=True)
    for lang in STRINGS:
        files[f"{main}/res/values-{lang}/strings.xml"] = strings_xml(lang)
    subs = {
        "@@PACKAGE@@": PACKAGE,
        "@@KEYSTORE@@": Path(os.path.relpath(KEYSTORE, PROJECT / "app")).as_posix(),
        "@@KEY_ALIAS@@": KEY_ALIAS,
        "@@KEY_PASS@@": KEY_PASS,
    }
    for rel, content in files.items():
        for k, v in subs.items():
            content = content.replace(k, v)
        path = PROJECT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# Instalador / desinstalador de Windows (tkinter, se empaqueta con PyInstaller)
# --------------------------------------------------------------------------

INSTALLER_PY = r'''"""Nova Auto TV para Retroid Pocket Nova: @@MODE@@ (Windows). Creado por ElPim_."""

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import tkinter as tk
from tkinter import ttk

MODE = "@@MODE@@"
PACKAGE = "@@PACKAGE@@"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "NovaAutoTV")
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # archivos incluidos en el .exe
APK = os.path.join(BASE, "NovaAutoTV.apk")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: sin consolas emergentes
WAIT_SECONDS = 120
DRIVER_CHECK_AFTER = 8  # segundos sin ver la consola antes de buscar un driver ADB que falte
STEPS = 5

LANGS = [("en", "English"), ("es", "Español"), ("pt", "Português"), ("fr", "Français")]

T = {
    "es": {
        "title_install": "Instalar Nova Auto TV",
        "title_uninstall": "Desinstalar Nova Auto TV",
        "credit": "por ElPim_",
        "language": "Idioma:",
        "steps_install": "1. En la consola, activa las Opciones de desarrollador: Ajustes → Acerca del dispositivo → "
                         "pulsa 7 veces «Número de compilación».\n"
                         "2. Ajustes → Sistema → Opciones de desarrollador → activa «Depuración USB».\n"
                         "3. Conecta la consola al PC con un cable USB de datos.\n"
                         "4. Pulsa «Instalar» y acepta en la consola el aviso «¿Permitir depuración USB?».\n"
                         "5. ¿El PC no detecta la consola por USB? Usa «Conectar por Wi-Fi» (sin drivers).",
        "steps_uninstall": "1. Conecta la consola al PC con la Depuración USB activada.\n"
                           "2. Pulsa «Desinstalar». Se restaurará la pantalla, se quitará la app de la consola "
                           "y se borrarán de este PC los archivos que descargó el instalador.\n"
                           "3. ¿El PC no detecta la consola por USB? Usa «Conectar por Wi-Fi» (sin drivers).",
        "btn_install": "Instalar",
        "btn_uninstall": "Desinstalar",
        "btn_wifi": "Conectar por Wi-Fi",
        "wifi_title": "Conectar por Wi-Fi",
        "wifi_steps": "1. Conecta la consola y el PC a la misma red Wi-Fi.\n"
                      "2. En la consola: Ajustes → Sistema → Opciones de desarrollador → activa «Depuración "
                      "inalámbrica» y entra en ella.\n"
                      "3. Pulsa «Vincular dispositivo con código de vinculación».\n"
                      "4. La IP:puerto se rellena sola (si no, cópiala de la consola). Escribe el código de "
                      "6 dígitos y pulsa «Vincular y conectar».",
        "wifi_pair_addr": "IP:puerto de vinculación (p. ej. 192.168.1.20:37123)",
        "wifi_code": "Código de vinculación (6 dígitos)",
        "wifi_connect_addr": "«IP y puerto» de la pantalla Depuración inalámbrica (opcional, solo si no conecta sola)",
        "btn_pair": "Vincular y conectar",
        "wifi_bad_input": "Escribe la IP:puerto de vinculación y el código de 6 dígitos.",
        "pairing": "Vinculando…",
        "paired": "Vinculado.",
        "pair_failed": "No se pudo vincular. Revisa el código y la IP:puerto (cambian cada vez que abres "
                       "la ventana de vinculación). {}",
        "connecting": "Conectando con la consola…",
        "connect_failed": "No se pudo conectar. Escribe el «IP y puerto» que muestra la pantalla Depuración "
                          "inalámbrica y pulsa de nuevo.",
        "adb_download": "Descargando adb (Android platform-tools)…",
        "adb_using": "Usando adb: {}",
        "waiting": "Buscando la consola…",
        "unauthorized": "Acepta el aviso «¿Permitir depuración USB?» en la pantalla de la consola "
                        "(marca «Permitir siempre desde este ordenador»).",
        "no_device": "No se detecta ninguna consola. En la consola, desliza la barra de notificaciones → toca "
                     "«USB» → elige «Transferencia de archivos». Prueba otro cable de datos u otro puerto y "
                     "comprueba que la Depuración USB esté activada. O usa «Conectar por Wi-Fi».",
        "no_driver": "Windows ve la consola pero sin driver ADB, así que por USB no funcionará. "
                     "Usa «Conectar por Wi-Fi» (recomendado, no necesita drivers).",
        "timeout": "No se encontró la consola. Revisa la conexión y vuelve a intentarlo.",
        "device_ok": "Consola conectada: {}",
        "installing": "Instalando la app…",
        "incompatible": "Hay otra versión de la app firmada con otra clave. "
                        "Quítala primero con NovaAutoTV-Uninstall.exe.",
        "granting": "Concediendo el permiso para cambiar la resolución…",
        "launching": "Abriendo la app…",
        "done_install": "¡Listo! En la consola, activa «Servicio activo» en Nova Auto TV.",
        "resetting": "Restaurando la resolución original de la pantalla…",
        "removing": "Desinstalando la app de la consola…",
        "not_installed": "La app no estaba instalada en la consola.",
        "cleaning": "Borrando de este PC los archivos descargados…",
        "done_uninstall": "Listo. Nova Auto TV se ha eliminado.",
        "error": "Error: {}",
    },
    "en": {
        "title_install": "Install Nova Auto TV",
        "title_uninstall": "Uninstall Nova Auto TV",
        "credit": "by ElPim_",
        "language": "Language:",
        "steps_install": "1. On the console, enable Developer options: Settings → About device → "
                         "tap \"Build number\" 7 times.\n"
                         "2. Settings → System → Developer options → turn on \"USB debugging\".\n"
                         "3. Connect the console to the PC with a USB data cable.\n"
                         "4. Click \"Install\" and accept the \"Allow USB debugging?\" prompt on the console.\n"
                         "5. PC doesn't detect the console over USB? Use \"Connect over Wi-Fi\" (no drivers).",
        "steps_uninstall": "1. Connect the console to the PC with USB debugging enabled.\n"
                           "2. Click \"Uninstall\". The screen will be restored, the app removed from the console "
                           "and the files downloaded by the installer deleted from this PC.\n"
                           "3. PC doesn't detect the console over USB? Use \"Connect over Wi-Fi\" (no drivers).",
        "btn_install": "Install",
        "btn_uninstall": "Uninstall",
        "btn_wifi": "Connect over Wi-Fi",
        "wifi_title": "Connect over Wi-Fi",
        "wifi_steps": "1. Connect the console and the PC to the same Wi-Fi network.\n"
                      "2. On the console: Settings → System → Developer options → turn on \"Wireless "
                      "debugging\" and open it.\n"
                      "3. Tap \"Pair device with pairing code\".\n"
                      "4. The IP:port fills in by itself (if not, copy it from the console). Type the 6-digit "
                      "code and click \"Pair and connect\".",
        "wifi_pair_addr": "Pairing IP:port (e.g. 192.168.1.20:37123)",
        "wifi_code": "Pairing code (6 digits)",
        "wifi_connect_addr": "\"IP address & Port\" from the Wireless debugging screen (optional, only if it "
                             "doesn't connect by itself)",
        "btn_pair": "Pair and connect",
        "wifi_bad_input": "Type the pairing IP:port and the 6-digit code.",
        "pairing": "Pairing…",
        "paired": "Paired.",
        "pair_failed": "Pairing failed. Check the code and the IP:port (they change every time you open "
                       "the pairing window). {}",
        "connecting": "Connecting to the console…",
        "connect_failed": "Could not connect. Type the \"IP address & Port\" shown on the Wireless debugging "
                          "screen and click again.",
        "adb_download": "Downloading adb (Android platform-tools)…",
        "adb_using": "Using adb: {}",
        "waiting": "Looking for the console…",
        "unauthorized": "Accept the \"Allow USB debugging?\" prompt on the console screen "
                        "(tick \"Always allow from this computer\").",
        "no_device": "No console detected. On the console, pull down the notification shade → tap \"USB\" → "
                     "choose \"File transfer\". Try another data cable or USB port and check that USB debugging "
                     "is on. Or use \"Connect over Wi-Fi\".",
        "no_driver": "Windows sees the console but without an ADB driver, so USB won't work. "
                     "Use \"Connect over Wi-Fi\" (recommended, no drivers needed).",
        "timeout": "The console was not found. Check the connection and try again.",
        "device_ok": "Console connected: {}",
        "installing": "Installing the app…",
        "incompatible": "Another version of the app signed with a different key is installed. "
                        "Remove it first with NovaAutoTV-Uninstall.exe.",
        "granting": "Granting the permission to change the resolution…",
        "launching": "Opening the app…",
        "done_install": "Done! On the console, turn on \"Service active\" in Nova Auto TV.",
        "resetting": "Restoring the original screen resolution…",
        "removing": "Uninstalling the app from the console…",
        "not_installed": "The app was not installed on the console.",
        "cleaning": "Deleting the downloaded files from this PC…",
        "done_uninstall": "Done. Nova Auto TV has been removed.",
        "error": "Error: {}",
    },
    "pt": {
        "title_install": "Instalar Nova Auto TV",
        "title_uninstall": "Desinstalar Nova Auto TV",
        "credit": "por ElPim_",
        "language": "Idioma:",
        "steps_install": "1. No console, ative as Opções do desenvolvedor: Configurações → Sobre o dispositivo → "
                         "toque 7 vezes em \"Número da versão\".\n"
                         "2. Configurações → Sistema → Opções do desenvolvedor → ative \"Depuração USB\".\n"
                         "3. Conecte o console ao PC com um cabo USB de dados.\n"
                         "4. Clique em \"Instalar\" e aceite no console o aviso \"Permitir depuração USB?\".\n"
                         "5. O PC não detecta o console por USB? Use \"Conectar por Wi-Fi\" (sem drivers).",
        "steps_uninstall": "1. Conecte o console ao PC com a Depuração USB ativada.\n"
                           "2. Clique em \"Desinstalar\". A tela será restaurada, o app será removido do console "
                           "e os arquivos baixados pelo instalador serão apagados deste PC.\n"
                           "3. O PC não detecta o console por USB? Use \"Conectar por Wi-Fi\" (sem drivers).",
        "btn_install": "Instalar",
        "btn_uninstall": "Desinstalar",
        "btn_wifi": "Conectar por Wi-Fi",
        "wifi_title": "Conectar por Wi-Fi",
        "wifi_steps": "1. Conecte o console e o PC à mesma rede Wi-Fi.\n"
                      "2. No console: Configurações → Sistema → Opções do desenvolvedor → ative \"Depuração "
                      "por Wi-Fi\" e abra essa opção.\n"
                      "3. Toque em \"Parear o dispositivo com um código de pareamento\".\n"
                      "4. O IP:porta é preenchido sozinho (se não, copie do console). Digite o código de "
                      "6 dígitos e clique em \"Parear e conectar\".",
        "wifi_pair_addr": "IP:porta de pareamento (ex.: 192.168.1.20:37123)",
        "wifi_code": "Código de pareamento (6 dígitos)",
        "wifi_connect_addr": "\"Endereço IP e porta\" da tela Depuração por Wi-Fi (opcional, só se não conectar "
                             "sozinho)",
        "btn_pair": "Parear e conectar",
        "wifi_bad_input": "Digite o IP:porta de pareamento e o código de 6 dígitos.",
        "pairing": "Pareando…",
        "paired": "Pareado.",
        "pair_failed": "Não foi possível parear. Confira o código e o IP:porta (mudam toda vez que você abre "
                       "a janela de pareamento). {}",
        "connecting": "Conectando ao console…",
        "connect_failed": "Não foi possível conectar. Digite o \"Endereço IP e porta\" mostrado na tela "
                          "Depuração por Wi-Fi e clique de novo.",
        "adb_download": "Baixando o adb (Android platform-tools)…",
        "adb_using": "Usando adb: {}",
        "waiting": "Procurando o console…",
        "unauthorized": "Aceite o aviso \"Permitir depuração USB?\" na tela do console "
                        "(marque \"Sempre permitir neste computador\").",
        "no_device": "Nenhum console detectado. No console, deslize a barra de notificações → toque em \"USB\" → "
                     "escolha \"Transferência de arquivos\". Tente outro cabo de dados ou outra porta e verifique "
                     "se a Depuração USB está ativada. Ou use \"Conectar por Wi-Fi\".",
        "no_driver": "O Windows vê o console mas sem driver ADB, então por USB não vai funcionar. "
                     "Use \"Conectar por Wi-Fi\" (recomendado, não precisa de drivers).",
        "timeout": "O console não foi encontrado. Verifique a conexão e tente de novo.",
        "device_ok": "Console conectado: {}",
        "installing": "Instalando o app…",
        "incompatible": "Há outra versão do app assinada com outra chave. "
                        "Remova-a primeiro com o NovaAutoTV-Uninstall.exe.",
        "granting": "Concedendo a permissão para alterar a resolução…",
        "launching": "Abrindo o app…",
        "done_install": "Pronto! No console, ative \"Serviço ativo\" no Nova Auto TV.",
        "resetting": "Restaurando a resolução original da tela…",
        "removing": "Desinstalando o app do console…",
        "not_installed": "O app não estava instalado no console.",
        "cleaning": "Apagando deste PC os arquivos baixados…",
        "done_uninstall": "Pronto. O Nova Auto TV foi removido.",
        "error": "Erro: {}",
    },
    "fr": {
        "title_install": "Installer Nova Auto TV",
        "title_uninstall": "Désinstaller Nova Auto TV",
        "credit": "par ElPim_",
        "language": "Langue :",
        "steps_install": "1. Sur la console, activez les Options pour les développeurs : Paramètres → À propos de "
                         "l'appareil → appuyez 7 fois sur « Numéro de build ».\n"
                         "2. Paramètres → Système → Options pour les développeurs → activez « Débogage USB ».\n"
                         "3. Branchez la console au PC avec un câble USB de données.\n"
                         "4. Cliquez sur « Installer » et acceptez sur la console « Autoriser le débogage USB ? ».\n"
                         "5. Le PC ne détecte pas la console en USB ? Utilisez « Connecter en Wi-Fi » (sans pilotes).",
        "steps_uninstall": "1. Branchez la console au PC avec le débogage USB activé.\n"
                           "2. Cliquez sur « Désinstaller ». L'écran sera restauré, l'app retirée de la console "
                           "et les fichiers téléchargés par l'installateur supprimés de ce PC.\n"
                           "3. Le PC ne détecte pas la console en USB ? Utilisez « Connecter en Wi-Fi » (sans pilotes).",
        "btn_install": "Installer",
        "btn_uninstall": "Désinstaller",
        "btn_wifi": "Connecter en Wi-Fi",
        "wifi_title": "Connecter en Wi-Fi",
        "wifi_steps": "1. Connectez la console et le PC au même réseau Wi-Fi.\n"
                      "2. Sur la console : Paramètres → Système → Options pour les développeurs → activez "
                      "« Débogage sans fil » et ouvrez-le.\n"
                      "3. Appuyez sur « Associer l'appareil avec un code d'association ».\n"
                      "4. L'IP:port se remplit tout seul (sinon, copiez-le depuis la console). Saisissez le code "
                      "à 6 chiffres et cliquez sur « Associer et connecter ».",
        "wifi_pair_addr": "IP:port d'association (ex. 192.168.1.20:37123)",
        "wifi_code": "Code d'association (6 chiffres)",
        "wifi_connect_addr": "« Adresse IP et port » de l'écran Débogage sans fil (facultatif, seulement si la "
                             "connexion ne se fait pas seule)",
        "btn_pair": "Associer et connecter",
        "wifi_bad_input": "Saisissez l'IP:port d'association et le code à 6 chiffres.",
        "pairing": "Association…",
        "paired": "Associée.",
        "pair_failed": "Échec de l'association. Vérifiez le code et l'IP:port (ils changent à chaque ouverture "
                       "de la fenêtre d'association). {}",
        "connecting": "Connexion à la console…",
        "connect_failed": "Connexion impossible. Saisissez l'« Adresse IP et port » affichée sur l'écran "
                          "Débogage sans fil et cliquez à nouveau.",
        "adb_download": "Téléchargement d'adb (Android platform-tools)…",
        "adb_using": "adb utilisé : {}",
        "waiting": "Recherche de la console…",
        "unauthorized": "Acceptez « Autoriser le débogage USB ? » sur l'écran de la console "
                        "(cochez « Toujours autoriser sur cet ordinateur »).",
        "no_device": "Aucune console détectée. Sur la console, faites glisser la barre de notifications → touchez "
                     "« USB » → choisissez « Transfert de fichiers ». Essayez un autre câble de données ou un autre "
                     "port et vérifiez que le débogage USB est activé. Ou utilisez « Connecter en Wi-Fi ».",
        "no_driver": "Windows voit la console mais sans pilote ADB, l'USB ne fonctionnera donc pas. "
                     "Utilisez « Connecter en Wi-Fi » (recommandé, aucun pilote nécessaire).",
        "timeout": "Console introuvable. Vérifiez la connexion et réessayez.",
        "device_ok": "Console connectée : {}",
        "installing": "Installation de l'app…",
        "incompatible": "Une autre version de l'app signée avec une autre clé est installée. "
                        "Retirez-la d'abord avec NovaAutoTV-Uninstall.exe.",
        "granting": "Octroi de l'autorisation de changer la résolution…",
        "launching": "Ouverture de l'app…",
        "done_install": "Terminé ! Sur la console, activez « Service actif » dans Nova Auto TV.",
        "resetting": "Restauration de la résolution d'origine de l'écran…",
        "removing": "Désinstallation de l'app de la console…",
        "not_installed": "L'app n'était pas installée sur la console.",
        "cleaning": "Suppression des fichiers téléchargés sur ce PC…",
        "done_uninstall": "Terminé. Nova Auto TV a été supprimée.",
        "error": "Erreur : {}",
    },
}


def system_lang():
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except Exception:
        return "en"
    return {0x0A: "es", 0x09: "en", 0x16: "pt", 0x0C: "fr"}.get(primary, "en")


class Failed(Exception):
    """Error con mensaje traducible: Failed(clave, *argumentos)."""


class Cancelled(Exception):
    """La espera por USB se cancelo porque el usuario eligio conectar por Wi-Fi."""


# ---- Aspecto: tema oscuro plano, solo con tkinter ----

BG = "#0f1117"
CARD = "#191c25"
CARD_HI = "#252a37"
TEXT = "#eef0f6"
MUTED = "#8b92a6"
ACCENT = "#6c8cff" if MODE == "install" else "#ff5f73"
ACCENT_HOVER = "#8aa3ff" if MODE == "install" else "#ff7d8e"
GOOD = "#4ade80"
BAD = "#ff6b6b"
FONT = "Segoe UI"
MONO = "Consolas"


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    """tkinter no tiene border-radius: poligono suavizado con las esquinas duplicadas."""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def dark_titlebar(win):
    """Barra de titulo oscura en Windows 10/11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


class RoundButton(tk.Canvas):
    """Boton plano con esquinas redondeadas y efecto hover (relleno, o solo contorno si outline=True)."""

    def __init__(self, master, command, height=48, radius=14, outline=False):
        super().__init__(master, height=height, bg=master["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.radius = radius
        self.outline = outline
        self.text = ""
        self.enabled = True
        self.hover = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self.set_hover(True))
        self.bind("<Leave>", lambda e: self.set_hover(False))
        self.bind("<ButtonRelease-1>", self.on_click)

    def set_text(self, text):
        self.text = text
        self.draw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.draw()

    def set_hover(self, hover):
        self.hover = hover
        self.draw()

    def on_click(self, e):
        if self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2:
            return
        if self.outline:
            border = ACCENT if self.enabled else CARD_HI
            fill = CARD_HI if self.enabled and self.hover else self["bg"]
            fg = (ACCENT_HOVER if self.hover else ACCENT) if self.enabled else MUTED
            rounded_rect(self, 2, 2, w - 2, h - 2, self.radius, fill=fill, outline=border, width=2)
            self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=(FONT, 11, "bold"))
            return
        if not self.enabled:
            fill, fg = CARD_HI, MUTED
        else:
            fill, fg = (ACCENT_HOVER if self.hover else ACCENT), "#ffffff"
        rounded_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=(FONT, 12, "bold"))


class App:
    def __init__(self, root):
        self.root = root
        self.lang = system_lang()
        self.events = queue.Queue()
        self.adb_path = None
        self.serial = None
        self.egg_win = None
        self.worker = None
        self.adb_lock = threading.Lock()
        self.cancel_wait = threading.Event()  # el boton Wi-Fi corta la espera por USB
        self.wifi_win = None
        self.wifi_auto = ""  # ultima IP:puerto de vinculacion rellenada por mDNS
        self.paired_addr = None

        root.configure(bg=BG)
        root.geometry("620x800")
        root.minsize(560, 720)
        self.setup_style()
        self.set_icon()

        outer = tk.Frame(root, bg=BG, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        # Cabecera: logo, titulo + credito, idioma
        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        logo = tk.Canvas(top, width=52, height=52, bg=BG, highlightthickness=0)
        rounded_rect(logo, 1, 1, 51, 51, 16, fill=ACCENT, outline="")
        logo.create_text(26, 26, text="16:9", fill="#ffffff", font=(FONT, 11, "bold"))
        logo.pack(side="left")
        titles = tk.Frame(top, bg=BG)
        titles.pack(side="left", padx=14)
        self.header = tk.Label(titles, bg=BG, fg=TEXT, font=(FONT, 18, "bold"), anchor="w")
        self.header.pack(anchor="w")
        self.credit = tk.Label(titles, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w")
        self.credit.pack(anchor="w")
        self.credit.bind("<Button-1>", self.egg)

        lang_col = tk.Frame(top, bg=BG)
        lang_col.pack(side="right")
        self.lang_label = tk.Label(lang_col, bg=BG, fg=MUTED, font=(FONT, 9))
        self.lang_label.pack(anchor="e")
        self.lang_box = ttk.Combobox(lang_col, state="readonly", width=12, style="Dark.TCombobox",
                                     values=[name for _, name in LANGS], font=(FONT, 10))
        self.lang_box.current([code for code, _ in LANGS].index(self.lang))
        self.lang_box.bind("<<ComboboxSelected>>", self.on_lang)
        self.lang_box.pack(anchor="e")

        # Pasos numerados en una tarjeta
        self.steps_box = tk.Frame(outer, bg=CARD, padx=18, pady=14)
        self.steps_box.pack(fill="x", pady=(20, 16))

        self.button = RoundButton(outer, self.start)
        self.button.pack(fill="x")
        self.wifi_button = RoundButton(outer, self.open_wifi, height=40, radius=12, outline=True)
        self.wifi_button.pack(fill="x", pady=(10, 0))

        # Estado + progreso
        self.status = tk.Label(outer, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w", justify="left")
        self.status.pack(fill="x", pady=(16, 6))
        self.status.bind("<Configure>", lambda e: self.status.configure(wraplength=max(e.width - 4, 100)))
        self.progress = ttk.Progressbar(outer, maximum=STEPS, style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x")

        # Registro
        log_card = tk.Frame(outer, bg=CARD)
        log_card.pack(fill="both", expand=True, pady=(14, 0))
        self.log_box = tk.Text(log_card, height=8, bg=CARD, fg=MUTED, font=(MONO, 9), relief="flat", bd=0,
                               highlightthickness=0, wrap="word", padx=12, pady=10, state="disabled",
                               cursor="arrow", insertbackground=CARD)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("info", foreground=MUTED)
        self.log_box.tag_configure("good", foreground=GOOD)
        self.log_box.tag_configure("bad", foreground=BAD)

        self.apply_texts()
        dark_titlebar(root)
        root.after(100, self.poll)

    # ---- interfaz (solo en el hilo de tkinter) ----

    def setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=CARD, background=ACCENT,
                        bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        style.configure("Dark.TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                        arrowcolor=MUTED, bordercolor=CARD_HI, lightcolor=CARD, darkcolor=CARD,
                        selectbackground=CARD, selectforeground=TEXT, padding=4)
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", CARD), ("disabled", BG)],
                  foreground=[("disabled", MUTED), ("readonly", TEXT)],
                  background=[("active", CARD_HI)],
                  arrowcolor=[("active", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    def set_icon(self):
        """Icono de ventana: una pequena TV en el color de acento."""
        img = tk.PhotoImage(width=32, height=32)
        img.put(ACCENT, to=(2, 4, 30, 24))
        img.put(BG, to=(5, 7, 27, 21))
        img.put(ACCENT, to=(12, 25, 20, 28))
        self.icon = img
        self.root.iconphoto(True, img)

    def t(self, key, *args):
        return T[self.lang][key].format(*args)

    def apply_texts(self):
        title = self.t("title_" + MODE)
        self.root.title(title)
        self.header.configure(text=title)
        self.credit.configure(text=self.t("credit"))
        self.lang_label.configure(text=self.t("language"))
        self.button.set_text(self.t("btn_" + MODE))
        self.wifi_button.set_text(self.t("btn_wifi"))
        self.render_steps()

    def render_steps(self):
        for w in self.steps_box.winfo_children():
            w.destroy()
        for i, line in enumerate(self.t("steps_" + MODE).split("\n"), 1):
            text = line.split(". ", 1)[1] if line[:1].isdigit() and ". " in line else line
            row = tk.Frame(self.steps_box, bg=CARD)
            row.pack(fill="x", pady=5)
            badge = tk.Canvas(row, width=26, height=26, bg=CARD, highlightthickness=0)
            badge.create_oval(1, 1, 25, 25, fill=ACCENT, outline="")
            badge.create_text(13, 13, text=str(i), fill="#ffffff", font=(FONT, 9, "bold"))
            badge.pack(side="left", anchor="n", padx=(0, 12))
            label = tk.Label(row, text=text, bg=CARD, fg=TEXT, font=(FONT, 10), justify="left",
                             anchor="w", wraplength=440)
            label.pack(side="left", fill="x", expand=True)
            label.bind("<Configure>", lambda e, l=label: l.configure(wraplength=max(e.width - 4, 100)))

    def on_lang(self, _event):
        self.lang = LANGS[self.lang_box.current()][0]
        self.apply_texts()

    def egg(self, _event=None):
        if self.egg_win is not None:
            self.egg_win.lift()
            return
        path = os.path.join(BASE, "grain.png")
        if not os.path.exists(path):
            return
        win = tk.Toplevel(self.root, bg=BG)
        win.title("ElPim_")
        win.resizable(False, False)
        win.transient(self.root)
        self.egg_img = tk.PhotoImage(file=path)
        tk.Label(win, image=self.egg_img, bg=BG, bd=0).pack(padx=14, pady=(14, 8))
        tk.Label(win, text="ElPim_  ♥", bg=BG, fg=ACCENT, font=(FONT, 11, "bold")).pack(pady=(0, 12))

        def close(_e=None):
            win.destroy()
            self.egg_win = None

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", close)
        win.bind("<Button-1>", close)
        dark_titlebar(win)
        self.egg_win = win

    def open_wifi(self):
        """Dialogo de Depuracion inalambrica: adb pair + adb connect, sin drivers USB."""
        if self.wifi_win is not None:
            self.wifi_win.lift()
            return
        self.cancel_wait.set()
        win = tk.Toplevel(self.root, bg=BG, padx=22, pady=18)
        win.title(self.t("wifi_title"))
        win.resizable(False, False)
        win.transient(self.root)
        tk.Label(win, text=self.t("wifi_title"), bg=BG, fg=TEXT, font=(FONT, 14, "bold"),
                 anchor="w").pack(fill="x")
        card = tk.Frame(win, bg=CARD, padx=16, pady=12)
        card.pack(fill="x", pady=(12, 8))
        tk.Label(card, text=self.t("wifi_steps"), bg=CARD, fg=TEXT, font=(FONT, 10), justify="left",
                 anchor="w", wraplength=470).pack(fill="x")
        entries = []
        for key in ("wifi_pair_addr", "wifi_code", "wifi_connect_addr"):
            tk.Label(win, text=self.t(key), bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=500).pack(fill="x", pady=(8, 3))
            entry = tk.Entry(win, bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat", font=(MONO, 12),
                             highlightthickness=1, highlightbackground=CARD_HI, highlightcolor=ACCENT)
            entry.pack(fill="x", ipady=6)
            entries.append(entry)
        self.wifi_addr, self.wifi_code, self.wifi_connect = entries
        self.wifi_msg = tk.Label(win, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w", justify="left",
                                 wraplength=500)
        self.wifi_msg.pack(fill="x", pady=(12, 8))
        self.pair_button = RoundButton(win, self.pair, height=44)
        self.pair_button.set_text(self.t("btn_pair"))
        self.pair_button.pack(fill="x")

        win.protocol("WM_DELETE_WINDOW", self.close_wifi)
        win.bind("<Escape>", lambda e: self.close_wifi())
        self.wifi_code.bind("<Return>", lambda e: self.pair())
        dark_titlebar(win)
        self.wifi_win = win
        self.wifi_auto = ""
        self.wifi_code.focus_set()
        win.after(100, lambda: self.grab(win))
        threading.Thread(target=self.watch_mdns, args=(win,), daemon=True).start()

    def grab(self, win):
        try:
            win.grab_set()  # modal: no se puede pulsar Instalar mientras se vincula
        except tk.TclError:
            pass

    def close_wifi(self):
        if self.wifi_win is not None:
            self.wifi_win.destroy()
            self.wifi_win = None

    def pair(self):
        if not self.pair_button.enabled:
            return
        addr = self.wifi_addr.get().strip()
        code = "".join(self.wifi_code.get().split())
        connect = self.wifi_connect.get().strip()
        self.pair_button.set_enabled(False)
        threading.Thread(target=self.wifi_work, args=(addr, code, connect), daemon=True).start()

    def start_when_idle(self):
        """Tras conectar por Wi-Fi: espera a que termine la espera USB cancelada y lanza el flujo normal."""
        if self.worker is not None and self.worker.is_alive():
            self.root.after(200, self.start_when_idle)
        else:
            self.start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    text, level = value
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", text + "\n", level)
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                    self.status.configure(text=text, fg={"good": GOOD, "bad": BAD}.get(level, TEXT))
                elif kind == "progress":
                    self.progress["value"] = value
                elif kind == "done":
                    self.button.set_enabled(True)
                    self.wifi_button.set_enabled(True)
                    self.lang_box.configure(state="readonly")
                elif kind == "wifi_button":
                    self.wifi_button.set_enabled(value)
                elif self.wifi_win is None:
                    continue  # mensajes de un dialogo Wi-Fi ya cerrado
                elif kind == "wifi_msg":
                    text, level = value
                    self.wifi_msg.configure(text=text, fg={"good": GOOD, "bad": BAD}.get(level, TEXT))
                elif kind == "wifi_addr":
                    current = self.wifi_addr.get().strip()
                    if current in ("", self.wifi_auto) and current != value:
                        self.wifi_addr.delete(0, "end")
                        self.wifi_addr.insert(0, value)
                    self.wifi_auto = value
                elif kind == "wifi_idle":
                    self.pair_button.set_enabled(True)
                elif kind == "wifi_ok":
                    self.close_wifi()
                    self.start_when_idle()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def start(self):
        self.button.set_enabled(False)
        self.lang_box.configure(state="disabled")
        self.progress["value"] = 0
        self.cancel_wait.clear()
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    # ---- trabajo (hilo aparte; se comunica por self.events) ----

    def say(self, key, *args, level="info"):
        self.events.put(("log", (self.t(key, *args), level)))

    def step(self, n):
        self.events.put(("progress", n))

    def work(self):
        try:
            if MODE == "install":
                self.install()
            else:
                self.uninstall()
        except Cancelled:
            pass
        except Failed as e:
            self.say(*e.args, level="bad")
        except Exception as e:
            self.say("error", e, level="bad")
        self.events.put(("done", None))

    def adb_raw(self, *args, timeout=120):
        """adb sin -s: para comandos del servidor (devices, pair, connect, mdns)."""
        r = subprocess.run([self.adb_path] + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()

    def adb(self, *args, timeout=120):
        return self.adb_raw(*((["-s", self.serial] if self.serial else []) + list(args)), timeout=timeout)

    def ensure_adb(self):
        with self.adb_lock:  # el dialogo Wi-Fi y el flujo normal pueden pedirlo a la vez
            if self.adb_path and os.path.exists(self.adb_path):
                return
            local = os.path.join(APP_DIR, "platform-tools", "adb.exe")
            path = shutil.which("adb") or (local if os.path.exists(local) else None)
            if not path:
                self.say("adb_download")
                os.makedirs(APP_DIR, exist_ok=True)
                zip_path = os.path.join(APP_DIR, "platform-tools.zip")
                urllib.request.urlretrieve(PLATFORM_TOOLS_URL, zip_path)
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(APP_DIR)
                os.remove(zip_path)
                path = local
            self.adb_path = path
            self.say("adb_using", path)

    def driver_missing(self):
        """True si Windows tiene la consola enchufada pero sin driver ADB (aviso amarillo en el Administrador)."""
        if os.name != "nt":
            return False
        query = ("Get-PnpDevice -PresentOnly | Where-Object { $_.Status -ne 'OK' -and "
                 "$_.FriendlyName -match 'ADB|Android|Retroid|Nova' } | Select-Object -ExpandProperty FriendlyName")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                               creationflags=NO_WINDOW, timeout=30)
        except Exception:
            return False
        return bool(r.stdout.strip())

    def wait_device(self):
        self.say("waiting")
        self.serial = None
        self.adb("start-server", timeout=60)
        last = None
        started = time.time()
        driver_checked = no_driver = False
        while time.time() < started + WAIT_SECONDS:
            if self.cancel_wait.is_set():
                raise Cancelled()
            _, out = self.adb("devices")
            rows = [line.split("\t") for line in out.splitlines() if "\t" in line]
            ready = [serial for serial, state in rows if state == "device"]
            if ready:
                self.serial = ready[0]
                self.events.put(("wifi_button", False))
                self.say("device_ok", self.serial)
                return
            if not rows and not driver_checked and time.time() - started > DRIVER_CHECK_AFTER:
                driver_checked = True
                no_driver = self.driver_missing()
            if any(s == "unauthorized" for _, s in rows):
                state = "unauthorized"
            else:
                state = "no_driver" if no_driver else "no_device"
            if state != last:
                self.say(state)
                last = state
            time.sleep(2)
        raise Failed("timeout")

    # ---- Depuracion inalambrica (hilos del dialogo Wi-Fi) ----

    def wifi_say(self, key, *args, level="info"):
        self.events.put(("wifi_msg", (self.t(key, *args), level)))

    def mdns_services(self):
        """[(tipo, "ip:puerto")] de 'adb mdns services'; tipo es "pairing" o "connect"."""
        try:
            _, out = self.adb_raw("mdns", "services", timeout=10)
        except subprocess.TimeoutExpired:
            return []
        found = []
        for line in out.splitlines():
            m = re.search(r"_adb-tls-(pairing|connect)\._tcp.*?(\d+\.\d+\.\d+\.\d+:\d+)", line)
            if m:
                found.append((m.group(1), m.group(2)))
        return found

    def watch_mdns(self, win):
        """Mientras el dialogo este abierto, rellena la IP:puerto de vinculacion que anuncia la consola."""
        try:
            self.ensure_adb()
            self.adb_raw("start-server", timeout=60)
        except Exception as e:
            self.wifi_say("error", e, level="bad")
            return
        while self.wifi_win is win:
            pairing = [addr for kind, addr in self.mdns_services() if kind == "pairing"]
            if pairing:
                self.events.put(("wifi_addr", pairing[0]))
            time.sleep(2)

    def wifi_work(self, addr, code, connect):
        try:
            self.ensure_adb()
            if code or not self.paired_addr:
                if not re.fullmatch(r"[^\s:]+:\d+", addr) or not code:
                    raise Failed("wifi_bad_input")
                self.wifi_say("pairing")
                _, out = self.adb_raw("pair", addr, code, timeout=30)
                if "Successfully paired" not in out:
                    raise Failed("pair_failed", out)
                self.paired_addr = addr
                self.wifi_say("paired", level="good")
            self.wifi_say("connecting")
            if not connect:
                # La consola anuncia por mDNS su puerto de conexion; a veces adb ya se conecta solo.
                ip = self.paired_addr.rsplit(":", 1)[0]
                deadline = time.time() + 12
                while not connect and time.time() < deadline:
                    _, out = self.adb_raw("devices")
                    if any(line.endswith("\tdevice") for line in out.splitlines()):
                        self.events.put(("wifi_ok", None))
                        return
                    connect = next((a for kind, a in self.mdns_services()
                                    if kind == "connect" and a.rsplit(":", 1)[0] == ip), "")
                    if not connect:
                        time.sleep(2)
            if not connect:
                raise Failed("connect_failed")
            _, out = self.adb_raw("connect", connect, timeout=30)
            if "connected to" not in out:
                raise Failed("connect_failed")
            self.events.put(("wifi_ok", None))
        except Failed as e:
            self.wifi_say(*e.args, level="bad")
        except Exception as e:
            self.wifi_say("error", e, level="bad")
        finally:
            self.events.put(("wifi_idle", None))

    def install(self):
        self.ensure_adb()
        self.step(1)
        self.wait_device()
        self.step(2)

        self.say("installing")
        code, out = self.adb("install", "-r", APK, timeout=300)
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
            raise Failed("incompatible")
        if code != 0 or "Success" not in out:
            raise Failed("error", out)
        self.step(3)

        self.say("granting")
        code, out = self.adb("shell", "pm", "grant", PACKAGE, "android.permission.WRITE_SECURE_SETTINGS")
        if code != 0 or "Exception" in out:
            raise Failed("error", out)
        self.step(4)

        self.say("launching")
        self.adb("shell", "am", "start", "-n", PACKAGE + "/.MainActivity")
        self.step(5)
        self.say("done_install", level="good")

    def uninstall(self):
        self.ensure_adb()
        self.step(1)
        self.wait_device()
        self.step(2)

        self.say("resetting")
        self.adb("shell", "wm", "size", "reset")
        self.step(3)

        self.say("removing")
        _, out = self.adb("uninstall", PACKAGE)
        if "Success" not in out:
            self.say("not_installed")
        self.step(4)

        self.say("cleaning")
        self.serial = None
        self.adb("kill-server")
        time.sleep(1)
        shutil.rmtree(APP_DIR, ignore_errors=True)
        self.adb_path = None
        self.step(5)
        self.say("done_uninstall", level="good")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
'''


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(gradle, debug):
    task = "assembleDebug" if debug else "assembleRelease"
    log(f"Compilando ({task})")
    env = dict(os.environ, ANDROID_HOME=str(SDK_DIR), ANDROID_SDK_ROOT=str(SDK_DIR),
               GRADLE_USER_HOME=str(GRADLE_HOME))
    try:
        run([gradle, "-p", PROJECT, task, "--no-daemon", "--console=plain"], env=env)
    except subprocess.CalledProcessError:
        die("La compilacion con Gradle fallo (ver salida arriba).")

    variant = "debug" if debug else "release"
    apk_dir = PROJECT / "app" / "build" / "outputs" / "apk" / variant
    apks = sorted(apk_dir.glob("*.apk"))
    if not apks:
        die(f"No se encontro la APK en {apk_dir}")
    DIST.mkdir(exist_ok=True)
    shutil.copy2(apks[0], APK_OUT)
    log(f"APK lista: {APK_OUT}")


def build_exes():
    """Compila NovaAutoTV-Install.exe (con la APK dentro) y NovaAutoTV-Uninstall.exe bajo wine."""
    env = ensure_wine_python()
    if env is None:
        return
    BUILD_EXE.mkdir(exist_ok=True)
    shutil.copy2(APK_OUT, BUILD_EXE / APK_OUT.name)
    extras = []
    if EXTRA_IMAGE.exists():
        shutil.copy2(EXTRA_IMAGE, BUILD_EXE / EXTRA_IMAGE.name)
        extras = ["--add-data", f"{EXTRA_IMAGE.name};."]
    for mode, name in (("install", "NovaAutoTV-Install"), ("uninstall", "NovaAutoTV-Uninstall")):
        log(f"Compilando {name}.exe")
        src = BUILD_EXE / f"{name}.py"
        src.write_text(INSTALLER_PY.replace("@@MODE@@", mode).replace("@@PACKAGE@@", PACKAGE), encoding="utf-8")
        cmd = ["wine", PY_WIN_EXE, "-m", "PyInstaller", "--onefile", "--windowed", "--noconfirm",
               "--log-level", "WARN", "--name", name,
               "--distpath", "dist", "--workpath", "work", "--specpath", "."]
        if mode == "install":
            cmd += ["--add-data", f"{APK_OUT.name};."]
        cmd += extras
        cmd.append(src.name)
        try:
            run(cmd, cwd=BUILD_EXE, env=env)
        except subprocess.CalledProcessError:
            die(f"PyInstaller fallo al compilar {name}.exe (ver salida arriba).")
        shutil.copy2(BUILD_EXE / "dist" / f"{name}.exe", DIST / f"{name}.exe")
        log(f"Listo: {DIST / (name + '.exe')}")


def local_adb():
    return SDK_DIR / "platform-tools" / "adb"


def find_adb():
    """adb del PATH o del SDK local; si no hay, instala platform-tools con sdkmanager."""
    adb = shutil.which("adb")
    if adb:
        return adb
    if not local_adb().exists():
        ensure_java()
        TOOLCHAIN.mkdir(exist_ok=True)
        ensure_sdk()
        sdk_install(["platform-tools"])
        if not local_adb().exists():
            die("No se pudo instalar 'adb' (platform-tools).")
    return str(local_adb())


def install():
    log("Instalando en el dispositivo")
    try:
        run([find_adb(), "install", "-r", APK_OUT])
    except subprocess.CalledProcessError:
        die("adb install fallo. Si ya habia una version firmada con otra clave, desinstalala primero.")


def grant():
    """Concede (una sola vez, persiste tras reiniciar) el permiso para cambiar la resolucion."""
    log("Concediendo WRITE_SECURE_SETTINGS")
    try:
        run([find_adb(), "shell", "pm", "grant", PACKAGE, "android.permission.WRITE_SECURE_SETTINGS"])
    except subprocess.CalledProcessError:
        die("No se pudo conceder el permiso. Comprueba que la app esta instalada y la depuracion USB activa.")


def connected_devices(adb):
    out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    rows = [line.split("\t") for line in out.splitlines() if "\t" in line]
    for serial, state in rows:
        if state != "device":
            warn(f"Dispositivo {serial} en estado '{state}' (acepta el aviso de depuracion USB); se omite.")
    return [serial for serial, state in rows if state == "device"]


def uninstall_all(assume_yes):
    """Quita la app de los dispositivos conectados y borra del PC todo lo generado/descargado."""
    targets = [TOOLCHAIN, PROJECT, BUILD_EXE, DIST]
    print("Se quitara Nova Auto TV de la consola conectada (si la hay) y se borrara del PC:")
    for t in targets:
        print(f"   {t}")
    warn(f"Esto incluye la keystore de firma ({KEYSTORE}). Sin ella, una APK nueva no podra\n"
         "       actualizar las instalaciones existentes: habra que desinstalarlas antes.")
    if not assume_yes:
        try:
            answer = input("¿Continuar? [s/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("s", "si", "sí", "y", "yes"):
            log("Cancelado")
            return

    # No se descarga adb para esto: si no hay, no hay nada que quitar del dispositivo.
    adb = shutil.which("adb") or (str(local_adb()) if local_adb().exists() else None)
    if not adb:
        warn("No hay 'adb': se omite la desinstalacion en la consola.")
    else:
        devices = connected_devices(adb)
        if not devices:
            warn("No hay ninguna consola conectada: solo se limpia el PC.")
        for serial in devices:
            log(f"Quitando Nova Auto TV de {serial}")
            run([adb, "-s", serial, "shell", "wm", "size", "reset"], check=False)
            run([adb, "-s", serial, "uninstall", PACKAGE], check=False)
        if Path(adb) == local_adb():
            run([adb, "kill-server"], check=False)

    if WINE_PREFIX.exists() and shutil.which("wineserver"):
        subprocess.run(["wineserver", "-k"], env=wine_env(), check=False)
    for t in targets:
        if t.exists():
            log(f"Borrando {t}")
            shutil.rmtree(t, ignore_errors=True)
    log("Desinstalacion completa")


def main():
    ap = argparse.ArgumentParser(
        description="Compila la APK Nova Auto TV (4:3 <-> 16:9 automatico por HDMI) y sus instaladores "
                    "de Windows. Creada por ElPim_.")
    ap.add_argument("--install", action="store_true",
                    help="instala la APK con 'adb install -r' y concede el permiso WRITE_SECURE_SETTINGS")
    ap.add_argument("--grant", action="store_true",
                    help="solo concede el permiso WRITE_SECURE_SETTINGS a la app ya instalada (sin compilar)")
    ap.add_argument("--debug", action="store_true", help="compila la variante debug en lugar de release")
    ap.add_argument("--clean", action="store_true", help="borra android_project/, build_exe/ y dist/ antes de compilar")
    ap.add_argument("--no-exe", action="store_true", help="no compila los .exe de Windows (solo la APK)")
    ap.add_argument("--uninstall", action="store_true",
                    help="quita la app de la consola (restaurando la pantalla) y borra del PC .toolchain/, "
                         "android_project/, build_exe/ y dist/")
    ap.add_argument("--yes", action="store_true", help="con --uninstall, no pide confirmacion")
    args = ap.parse_args()

    if args.uninstall:
        uninstall_all(args.yes)
        return

    if args.grant:
        grant()
        return

    if args.clean:
        log("Limpiando android_project/, build_exe/ y dist/")
        for d in (PROJECT, BUILD_EXE, DIST):
            shutil.rmtree(d, ignore_errors=True)

    TOOLCHAIN.mkdir(exist_ok=True)
    ensure_java()
    ensure_sdk()
    gradle = ensure_gradle()
    if not args.debug:
        ensure_keystore()
    generate_project()
    build(gradle, args.debug)
    if args.install:
        install()
        grant()
    if not args.no_exe:
        build_exes()


if __name__ == "__main__":
    main()
