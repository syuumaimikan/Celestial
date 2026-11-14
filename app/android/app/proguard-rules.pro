# ML Kit の文字認識は、日本語(と英数字)だけを入れている。
# プラグインは中国語・韓国語・デーヴァナーガリーの認識器も参照するが、入れていないので R8 の警告を止める。
-dontwarn com.google.mlkit.vision.text.chinese.**
-dontwarn com.google.mlkit.vision.text.devanagari.**
-dontwarn com.google.mlkit.vision.text.korean.**
