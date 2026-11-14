package dev.celestial.celestial

import android.Manifest
import android.app.Activity
import android.app.ActivityManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.provider.Settings
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.view.WindowManager
import androidx.core.content.FileProvider
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.Locale

/**
 * Celestial の Android 側。Flutter から使う端末の機能(QR のリンク、共有、ファイル、通知など)をまとめる。
 */
class MainActivity : FlutterActivity() {
    private var pendingLaunch: HashMap<String, Any?>? = null
    private var intentSink: EventChannel.EventSink? = null
    private var pickResult: MethodChannel.Result? = null
    private var notificationId = 100
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var ttsPending: Pair<String, Int>? = null
    private var platformChannel: MethodChannel? = null
    @Volatile private var speechToken = 0       // いま読み上げている依頼の番号(Flutter 側が決める)
    @Volatile private var speechLast = ""       // その最後の区切りの ID(読み終わりの判定に使う)

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        pendingLaunch = parseIntent(intent)
        val channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "dev.celestial/platform")
        platformChannel = channel
        channel.setMethodCallHandler { call, result ->
            try {
                when (call.method) {
                    "launchData" -> {
                        result.success(pendingLaunch ?: HashMap<String, Any?>())
                        pendingLaunch = null
                    }
                    "deviceName" -> result.success(deviceName())
                    "deviceInfo" -> result.success(deviceInfo())
                    "appVersion" -> result.success(appVersion())
                    "installApk" -> result.success(installApk(call.argument<String>("path")!!))
                    "pickFile" -> pickFile(result)
                    "saveToDownloads" -> result.success(saveToDownloads(call.argument<String>("path")!!, call.argument<String>("name")!!))
                    "notify" -> {
                        notify(call.argument<String>("title") ?: "Celestial", call.argument<String>("body") ?: "")
                        result.success(null)
                    }
                    "openUrl" -> {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(call.argument<String>("url"))).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        result.success(null)
                    }
                    "shareText" -> {
                        val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, call.argument<String>("text") ?: "")
                        startActivity(Intent.createChooser(send, "共有").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        result.success(null)
                    }
                    "speak" -> {
                        speak(call.argument<String>("text") ?: "", call.argument<Int>("token") ?: 0)
                        result.success(null)
                    }
                    "listen" -> {
                        startListening(call.argument<String>("lang") ?: "ja-JP")
                        result.success(null)
                    }
                    "stopListening" -> {
                        recognizer?.stopListening()
                        result.success(null)
                    }
                    "cancelListening" -> {
                        recognizer?.cancel()
                        notifySpeech("end", "")
                        result.success(null)
                    }
                    "stopSpeaking" -> {
                        val token = speechToken
                        speechLast = ""
                        ttsPending = null
                        tts?.stop()
                        notifySpeaking(false, token)
                        result.success(null)
                    }
                    "keepScreenOn" -> {
                        if (call.argument<Boolean>("on") == true) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            } catch (e: Exception) {
                result.error("failed", e.message, null)
            }
        }
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, "dev.celestial/intents").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink?) {
                intentSink = events
            }

            override fun onCancel(arguments: Any?) {
                intentSink = null
            }
        })
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val data = parseIntent(intent) ?: return
        val sink = intentSink
        if (sink != null) sink.success(data) else pendingLaunch = data
    }

    /** celestial://pair?... のリンクや、ほかのアプリから共有された文字を取り出す。 */
    private fun parseIntent(intent: Intent?): HashMap<String, Any?>? {
        if (intent == null) return null
        return when (intent.action) {
            Intent.ACTION_VIEW -> intent.data?.toString()?.let { hashMapOf("link" to it) }
            Intent.ACTION_SEND -> {
                val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return null
                val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
                hashMapOf("text" to if (subject.isNullOrBlank()) text else "$subject\n$text")
            }
            else -> null
        }
    }

    private fun deviceName(): String {
        val name = if (Build.VERSION.SDK_INT >= 25) Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) else null
        return if (!name.isNullOrBlank()) name else "${Build.MANUFACTURER} ${Build.MODEL}"
    }

    private fun deviceInfo(): HashMap<String, Any?> {
        val memory = ActivityManager.MemoryInfo()
        (getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).getMemoryInfo(memory)
        return hashMapOf(
            "model" to "${Build.MANUFACTURER} ${Build.MODEL}",
            "android" to Build.VERSION.RELEASE,
            "sdk" to Build.VERSION.SDK_INT,
            "total_memory" to memory.totalMem,
            "available_memory" to memory.availMem,
            "abis" to Build.SUPPORTED_ABIS.toList(),
        )
    }

    // ---- アプリの更新(PC から受け取った新しい版を、Android の入れ替えの確認画面に渡す) ----

    private fun appVersion(): HashMap<String, Any?> {
        @Suppress("DEPRECATION")
        val info = packageManager.getPackageInfo(packageName, 0)
        @Suppress("DEPRECATION")
        val code = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        return hashMapOf("name" to info.versionName, "code" to code)
    }

    /** 入れる許可がまだ無ければ、その設定の画面を開いて "permission" を返す。確認画面を出せたら "started"。 */
    private fun installApk(path: String): String {
        if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls()) {
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return "permission"
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.files", File(path))
        startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        return "started"
    }

    // ---- ファイルの選択(選んだファイルは一時フォルダに写して、その場所を返す) ----

    private fun pickFile(result: MethodChannel.Result) {
        pickResult?.error("cancelled", "別の選択が始まりました", null)
        pickResult = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
        @Suppress("DEPRECATION")
        startActivityForResult(intent, REQUEST_PICK)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PICK) return
        val result = pickResult ?: return
        pickResult = null
        val uri = data?.data
        if (resultCode != Activity.RESULT_OK || uri == null) {
            result.success(null)
            return
        }
        Thread {
            try {
                var name = "file"
                contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                    if (c.moveToFirst()) name = c.getString(0) ?: name
                }
                val dir = File(cacheDir, "picked").apply { mkdirs() }
                val dest = File(dir, name.replace("/", "_"))
                contentResolver.openInputStream(uri)!!.use { input -> dest.outputStream().use { input.copyTo(it) } }
                runOnUiThread { result.success(hashMapOf("path" to dest.absolutePath, "name" to name, "size" to dest.length())) }
            } catch (e: Exception) {
                runOnUiThread { result.error("pick_failed", e.message, null) }
            }
        }.start()
    }

    // ---- 「ダウンロード」フォルダへの保存 ----

    private fun saveToDownloads(path: String, name: String): String {
        val source = File(path)
        if (Build.VERSION.SDK_INT >= 29) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: throw IllegalStateException("保存先を作れませんでした")
            contentResolver.openOutputStream(uri)!!.use { out -> source.inputStream().use { it.copyTo(out) } }
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            contentResolver.update(uri, values, null, null)
            return "Download/$name"
        }
        val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: filesDir
        val dest = File(dir, name)
        source.copyTo(dest, overwrite = true)
        return dest.absolutePath
    }

    // ---- 読み上げ(端末の音声合成を使う。初回だけ準備に少し時間がかかる) ----

    private fun speak(text: String, token: Int) {
        if (text.isBlank()) {
            notifySpeaking(false, token)
            return
        }
        val engine = tts
        if (engine == null || !ttsReady) {
            ttsPending = text to token
            if (engine == null) {
                tts = TextToSpeech(this) { status ->
                    ttsReady = status == TextToSpeech.SUCCESS
                    tts?.setOnUtteranceProgressListener(progress)
                    val waiting = ttsPending
                    ttsPending = null
                    if (waiting != null) {
                        if (ttsReady) speakNow(waiting.first, waiting.second) else notifySpeaking(false, waiting.second)
                    }
                }
            }
            return
        }
        speakNow(text, token)
    }

    private fun speakNow(text: String, token: Int) {
        val engine = tts ?: return
        engine.language = if (Regex("[\u3040-\u30ff\u4e00-\u9fff]").containsMatchIn(text)) Locale.JAPAN else Locale.US
        val parts = splitForSpeech(text, TextToSpeech.getMaxSpeechInputLength() - 1)
        speechToken = token
        speechLast = "celestial-$token-${parts.size - 1}"
        notifySpeaking(true, token)
        parts.forEachIndexed { i, chunk ->
            engine.speak(chunk, if (i == 0) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, "celestial-$token-$i")
        }
    }

    /** \u4e00\u5ea6\u306b\u8aad\u3081\u308b\u9577\u3055\u3054\u3068\u306b\u3001\u306a\u308b\u3079\u304f\u6587\u306e\u5207\u308c\u76ee(\u3002\u30fb\u6539\u884c\u30fb\u30d4\u30ea\u30aa\u30c9)\u3067\u5206\u3051\u308b\u3002 */
    private fun splitForSpeech(text: String, max: Int): List<String> {
        val parts = ArrayList<String>()
        var rest = text
        while (rest.length > max) {
            val window = rest.substring(0, max)
            var cut = maxOf(window.lastIndexOf('\u3002'), window.lastIndexOf('\n'), window.lastIndexOf(". "))
            if (cut < max / 2) cut = max - 1
            parts.add(rest.substring(0, cut + 1))
            rest = rest.substring(cut + 1)
        }
        if (rest.isNotBlank()) parts.add(rest)
        return parts
    }

    /** \u8aad\u307f\u7d42\u308f\u308a\u30fb\u5931\u6557\u3092 Flutter \u306b\u77e5\u3089\u305b\u308b(\u8aad\u307f\u4e0a\u3052\u306e\u30dc\u30bf\u30f3\u3092\u5143\u306b\u623b\u3059\u305f\u3081)\u3002\u524d\u306e\u4f9d\u983c\u306e\u77e5\u3089\u305b\u306f\u9001\u3089\u306a\u3044\u3002 */
    private val progress = object : UtteranceProgressListener() {
        override fun onStart(utteranceId: String?) {}

        override fun onDone(utteranceId: String?) {
            if (utteranceId != null && utteranceId == speechLast) notifySpeaking(false, speechToken)
        }

        @Deprecated("Deprecated in Java")
        override fun onError(utteranceId: String?) {
            if (utteranceId != null && utteranceId.startsWith("celestial-$speechToken-")) notifySpeaking(false, speechToken)
        }

        override fun onError(utteranceId: String?, errorCode: Int) {
            if (utteranceId != null && utteranceId.startsWith("celestial-$speechToken-")) notifySpeaking(false, speechToken)
        }
    }

    private fun notifySpeaking(on: Boolean, token: Int) {
        runOnUiThread { platformChannel?.invokeMethod("speaking", hashMapOf("on" to on, "token" to token)) }
    }

    // ---- 音声入力(マイクのボタンを押したときだけ。できれば端末の中だけで文字にする) ----

    private var recognizer: SpeechRecognizer? = null
    private var pendingListen: String? = null

    private fun startListening(lang: String, useService: Boolean = false) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingListen = lang
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_AUDIO)
            return
        }
        // 端末の中で文字にできるなら、それを使う(声を外へ送らない)。できなければ、端末の音声認識サービスにオフラインを優先して頼む
        val onDevice = !useService && Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(this)
        if (!onDevice && !SpeechRecognizer.isRecognitionAvailable(this)) {
            notifySpeech("error", "この端末では音声認識を使えません")
            return
        }
        recognizer?.destroy()
        val r = if (onDevice && Build.VERSION.SDK_INT >= 31) SpeechRecognizer.createOnDeviceSpeechRecognizer(this) else SpeechRecognizer.createSpeechRecognizer(this)
        recognizer = r
        r.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) = notifySpeech("ready", if (onDevice) "device" else "service")
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onError(error: Int) {
                // 端末の中の認識に、この言語のデータがない(12: 未対応 / 13: データなし)なら、端末の標準の音声認識
                // (オフラインを優先)に切り替えて続ける
                if (onDevice && (error == 12 || error == 13) && SpeechRecognizer.isRecognitionAvailable(this@MainActivity)) {
                    window.decorView.post { startListening(lang, useService = true) }
                    return
                }
                if (error == SpeechRecognizer.ERROR_CLIENT) notifySpeech("end", "") else notifySpeech("error", speechError(error))
            }
            override fun onResults(results: Bundle?) =
                notifySpeech("final", results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: "")
            override fun onPartialResults(partial: Bundle?) {
                val text = partial?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: return
                if (text.isNotEmpty()) notifySpeech("partial", text)
            }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        r.startListening(intent)
    }

    private fun speechError(code: Int): String = when (code) {
        SpeechRecognizer.ERROR_NO_MATCH -> "聞き取れませんでした。もう一度話してください"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "声が聞こえませんでした"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "マイクの使用が許可されていません"
        SpeechRecognizer.ERROR_AUDIO -> "マイクから音を取れませんでした"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "音声認識が使用中です。少し待ってからもう一度試してください"
        SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT, 12, 13 ->
            "音声認識を使えませんでした。端末の設定で、日本語の音声認識データ(オフライン用)を入れてください"
        else -> "音声認識でエラーが起きました($code)"
    }

    private fun notifySpeech(type: String, text: String) {
        runOnUiThread { platformChannel?.invokeMethod("speech", hashMapOf("type" to type, "text" to text)) }
    }

    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_AUDIO) return
        val lang = pendingListen
        pendingListen = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && lang != null) startListening(lang)
        else notifySpeech("error", "マイクの使用が許可されていません")
    }

    override fun onDestroy() {
        recognizer?.destroy()
        recognizer = null
        tts?.shutdown()
        tts = null
        super.onDestroy()
    }

    // ---- 通知 ----

    private fun notify(title: String, body: String) {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFY)
            return
        }
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(NotificationChannel(CHANNEL, "Celestial", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "回答・確認・PC からの送信のお知らせ"
            })
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        @Suppress("DEPRECATION")
        val builder = if (Build.VERSION.SDK_INT >= 26) android.app.Notification.Builder(this, CHANNEL) else android.app.Notification.Builder(this)
        val notification = builder
            .setSmallIcon(R.drawable.ic_stat_celestial)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(android.app.Notification.BigTextStyle().bigText(body))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        manager.notify(notificationId++, notification)
    }

    companion object {
        private const val REQUEST_PICK = 4101
        private const val REQUEST_NOTIFY = 4102
        private const val REQUEST_AUDIO = 4103
        private const val CHANNEL = "celestial"
    }
}
