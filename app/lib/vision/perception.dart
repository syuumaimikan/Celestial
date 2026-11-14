import 'dart:io';
import 'dart:ui';

import 'package:camera/camera.dart';
import 'package:flutter/foundation.dart';
import 'package:google_mlkit_barcode_scanning/google_mlkit_barcode_scanning.dart';
import 'package:google_mlkit_face_detection/google_mlkit_face_detection.dart';
import 'package:google_mlkit_image_labeling/google_mlkit_image_labeling.dart';
import 'package:google_mlkit_object_detection/google_mlkit_object_detection.dart';
import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';
import 'package:google_mlkit_translation/google_mlkit_translation.dart';

/// 端末の中だけで行う画像の認識(ML Kit)。カメラの映像は外へ送らない。
enum SeeMode {
  text('文字'),
  scene('物と様子'),
  code('QR・コード'),
  face('顔'),
  translate('翻訳'),
  ask('質問');

  const SeeMode(this.label);
  final String label;
}

/// 1枚の画像から読み取ったもの。座標は、向きを直した画像(upright)の上の位置。
class Seen {
  Seen({
    required this.size,
    this.text,
    this.objects = const [],
    this.labels = const [],
    this.barcodes = const [],
    this.faces = const [],
    this.millis = 0,
  });

  final Size size;
  final RecognizedText? text;
  final List<DetectedObject> objects;
  final List<ImageLabel> labels;
  final List<Barcode> barcodes;
  final List<Face> faces;
  final int millis;

  bool get isEmpty =>
      (text == null || text!.text.trim().isEmpty) && objects.isEmpty && labels.isEmpty && barcodes.isEmpty && faces.isEmpty;

  String get plainText => text?.text.trim() ?? '';

  /// 端末内のAIに渡す「読み取れたこと」の文章(このAIは画像を見られないので、読み取り結果を根拠に答える)。
  String describe() {
    final lines = <String>[];
    if (labels.isNotEmpty) {
      lines.add('- 写っているもの(推定): ${labels.take(8).map((l) => '${labelJa(l.label)}(${(l.confidence * 100).round()}%)').join('、')}');
    }
    if (objects.isNotEmpty) {
      final kinds = <String, int>{};
      for (final o in objects) {
        final name = o.labels.isEmpty ? '物' : objectJa(o.labels.first.text);
        kinds[name] = (kinds[name] ?? 0) + 1;
      }
      lines.add('- 見つけた物: ${objects.length} 個(${kinds.entries.map((e) => '${e.key} ${e.value}').join('、')})');
    }
    if (faces.isNotEmpty) {
      final smiling = faces.where((f) => (f.smilingProbability ?? 0) > 0.6).length;
      lines.add('- 人の顔: ${faces.length} 人${smiling > 0 ? '(笑顔 $smiling 人)' : ''}');
    }
    for (final b in barcodes.take(4)) {
      lines.add('- ${barcodeKind(b)}: ${b.displayValue ?? b.rawValue ?? ''}');
    }
    final t = plainText;
    if (t.isNotEmpty) lines.add('- 画像の中の文字:\n${t.length > 1200 ? '${t.substring(0, 1200)}…' : t}');
    return lines.isEmpty ? '(読み取れたものはありません)' : lines.join('\n');
  }
}

String barcodeKind(Barcode b) => switch (b.type) {
      BarcodeType.url => 'URL',
      BarcodeType.wifi => 'Wi-Fi',
      BarcodeType.email => 'メール',
      BarcodeType.phone => '電話番号',
      BarcodeType.sms => 'SMS',
      BarcodeType.geoCoordinates => '位置',
      BarcodeType.contactInfo => '連絡先',
      BarcodeType.calendarEvent => '予定',
      BarcodeType.product || BarcodeType.isbn => '商品コード',
      _ => b.format == BarcodeFormat.qrCode ? 'QR コード' : 'バーコード',
    };

/// ML Kit の物体の分類(基本のモデルの5種類)の日本語名。
String objectJa(String label) => const {
      'Fashion good': 'ファッション',
      'Food': '食べ物',
      'Home good': '家庭用品',
      'Place': '場所',
      'Plant': '植物',
    }[label] ??
    label;

/// ML Kit の画像のラベル(英語)の日本語名。よく出るものだけを訳し、ほかは英語のまま出す。
String labelJa(String label) => _labels[label] ?? label;

const _labels = {
  'Person': '人', 'People': '人々', 'Selfie': '自撮り', 'Smile': '笑顔', 'Hand': '手', 'Hair': '髪', 'Beard': 'ひげ', 'Eyelash': 'まつ毛',
  'Dog': '犬', 'Cat': '猫', 'Bird': '鳥', 'Fish': '魚', 'Horse': '馬', 'Insect': '虫', 'Butterfly': '蝶', 'Pet': 'ペット', 'Animal': '動物',
  'Food': '食べ物', 'Fruit': '果物', 'Vegetable': '野菜', 'Bread': 'パン', 'Cake': 'ケーキ', 'Cookie': 'クッキー', 'Pizza': 'ピザ',
  'Sushi': '寿司', 'Noodle': '麺', 'Rice': 'ご飯', 'Meat': '肉', 'Egg': '卵', 'Coffee': 'コーヒー', 'Tea': 'お茶', 'Drink': '飲み物',
  'Juice': 'ジュース', 'Wine': 'ワイン', 'Beer': 'ビール', 'Dessert': 'デザート', 'Lunch': '昼食', 'Dinner': '夕食', 'Breakfast': '朝食',
  'Tableware': '食器', 'Cup': 'カップ', 'Bowl': '茶わん・ボウル', 'Plate': '皿', 'Chopsticks': '箸', 'Bottle': 'ボトル',
  'Car': '車', 'Bicycle': '自転車', 'Motorcycle': 'バイク', 'Bus': 'バス', 'Train': '電車', 'Airplane': '飛行機', 'Boat': '船',
  'Vehicle': '乗り物', 'Wheel': '車輪', 'Road': '道路', 'Bridge': '橋', 'Building': '建物', 'House': '家', 'Skyscraper': '高層ビル',
  'Tower': '塔', 'Temple': '寺', 'Shrine': '神社', 'Castle': '城', 'City': '街', 'Room': '部屋', 'Kitchen': '台所', 'Bathroom': '浴室',
  'Bedroom': '寝室', 'Office': 'オフィス', 'Classroom': '教室', 'Restaurant': '飲食店', 'Shop': '店', 'Supermarket': 'スーパー',
  'Sky': '空', 'Cloud': '雲', 'Sun': '太陽', 'Moon': '月', 'Star': '星', 'Mountain': '山', 'Beach': '浜辺', 'Sea': '海', 'Lake': '湖',
  'River': '川', 'Waterfall': '滝', 'Snow': '雪', 'Rain': '雨', 'Tree': '木', 'Flower': '花', 'Plant': '植物', 'Grass': '草',
  'Garden': '庭', 'Forest': '森', 'Leaf': '葉', 'Sunset': '夕焼け', 'Night': '夜', 'Fireworks': '花火',
  'Computer': 'パソコン', 'Laptop': 'ノートパソコン', 'Keyboard': 'キーボード', 'Mouse': 'マウス', 'Monitor': 'モニター',
  'Screen': '画面', 'Mobile phone': '携帯電話', 'Television': 'テレビ', 'Camera': 'カメラ', 'Headphones': 'ヘッドホン',
  'Musical instrument': '楽器', 'Guitar': 'ギター', 'Piano': 'ピアノ', 'Book': '本', 'Paper': '紙', 'Poster': 'ポスター',
  'Newspaper': '新聞', 'Menu': 'メニュー', 'Receipt': 'レシート', 'Handwriting': '手書き', 'Text': '文字', 'Font': '書体',
  'Table': '机', 'Desk': '机', 'Chair': '椅子', 'Couch': 'ソファ', 'Bed': 'ベッド', 'Shelf': '棚', 'Window': '窓', 'Door': 'ドア',
  'Lamp': 'ランプ', 'Clock': '時計', 'Watch': '腕時計', 'Mirror': '鏡', 'Curtain': 'カーテン', 'Toy': 'おもちゃ', 'Doll': '人形',
  'Bag': 'かばん', 'Shoe': '靴', 'Hat': '帽子', 'Glasses': '眼鏡', 'Sunglasses': 'サングラス', 'Jacket': '上着', 'Shirt': 'シャツ',
  'Jeans': 'ジーンズ', 'Dress': 'ドレス', 'Umbrella': '傘', 'Bag (hand)': 'ハンドバッグ', 'Jewelry': '宝飾品', 'Necklace': 'ネックレス',
  'Sports': 'スポーツ', 'Ball': 'ボール', 'Soccer': 'サッカー', 'Baseball': '野球', 'Tennis': 'テニス', 'Skiing': 'スキー',
  'Swimming': '水泳', 'Running': 'ランニング', 'Stadium': '競技場', 'Game': 'ゲーム', 'Cool': 'かっこいい', 'Fun': '楽しい',
  'Event': '催し', 'Concert': 'コンサート', 'Party': 'パーティー', 'Wedding': '結婚式', 'Baby': '赤ちゃん', 'Child': '子ども',
  'Crowd': '人混み', 'Team': 'チーム', 'Pattern': '模様', 'Art': '芸術', 'Painting': '絵', 'Sculpture': '彫刻', 'Statue': '像',
  'Flag': '旗', 'Sign': '看板', 'Signage': '標識', 'Traffic light': '信号機', 'Stairs': '階段', 'Escalator': 'エスカレーター',
  'Elevator': 'エレベーター', 'Box': '箱', 'Container': '容器', 'Tool': '道具', 'Machine': '機械', 'Pen': 'ペン', 'Scissors': 'はさみ',
};

/// 画像の認識の道具をまとめたもの(使うときに作り、終わったら閉じる)。
class Perceiver {
  TextRecognizer? _text;
  ObjectDetector? _liveObjects;
  ObjectDetector? _stillObjects;
  ImageLabeler? _labels;
  BarcodeScanner? _codes;
  FaceDetector? _faces;
  OnDeviceTranslator? _translator;
  String _translatorKey = '';

  TextRecognizer get _textR => _text ??= TextRecognizer(script: TextRecognitionScript.japanese); // 日本語のほか英数字も読める

  ObjectDetector _objectsR(bool live) => live
      ? (_liveObjects ??= ObjectDetector(options: ObjectDetectorOptions(mode: DetectionMode.stream, classifyObjects: true, multipleObjects: true)))
      : (_stillObjects ??= ObjectDetector(options: ObjectDetectorOptions(mode: DetectionMode.single, classifyObjects: true, multipleObjects: true)));

  ImageLabeler get _labelsR => _labels ??= ImageLabeler(options: ImageLabelerOptions(confidenceThreshold: 0.55));
  BarcodeScanner get _codesR => _codes ??= BarcodeScanner();
  FaceDetector get _facesR => _faces ??= FaceDetector(
      options: FaceDetectorOptions(enableClassification: true, enableTracking: true, performanceMode: FaceDetectorMode.fast, minFaceSize: 0.12));

  /// モードに合わせて読み取る。live は映像の1コマ(速さ優先)、そうでなければ写真(全部読む)。
  Future<Seen> look(InputImage image, Size uprightSize, SeeMode mode, {bool live = true}) async {
    final watch = Stopwatch()..start();
    RecognizedText? text;
    var objects = <DetectedObject>[];
    var labels = <ImageLabel>[];
    var barcodes = <Barcode>[];
    var faces = <Face>[];
    final everything = !live; // 写真(質問・止めた画面)は、全部の読み取りをまとめて行う
    if (mode == SeeMode.text || mode == SeeMode.translate || everything) text = await _textR.processImage(image);
    if (mode == SeeMode.scene || everything) {
      objects = await _objectsR(live).processImage(image);
      labels = await _labelsR.processImage(image);
    }
    if (mode == SeeMode.code || everything) barcodes = await _codesR.processImage(image);
    if (mode == SeeMode.face || everything) faces = await _facesR.processImage(image);
    labels.sort((a, b) => b.confidence.compareTo(a.confidence));
    return Seen(
        size: uprightSize, text: text, objects: objects, labels: labels, barcodes: barcodes, faces: faces, millis: watch.elapsedMilliseconds);
  }

  /// 日本語なら英語へ、それ以外なら日本語へ訳す(端末の中の翻訳モデル)。
  Future<String> translate(String text) async {
    final japanese = hasJapanese(text);
    final source = japanese ? TranslateLanguage.japanese : TranslateLanguage.english;
    final target = japanese ? TranslateLanguage.english : TranslateLanguage.japanese;
    final key = '${source.bcpCode}>${target.bcpCode}';
    if (_translator == null || _translatorKey != key) {
      await _translator?.close();
      _translator = OnDeviceTranslator(sourceLanguage: source, targetLanguage: target);
      _translatorKey = key;
    }
    return _translator!.translateText(text);
  }

  Future<void> close() async {
    for (final f in [
      _text?.close(), _liveObjects?.close(), _stillObjects?.close(), _labels?.close(), _codes?.close(), _faces?.close(), _translator?.close()
    ]) {
      try {
        await f;
      } catch (_) {}
    }
    _text = null;
    _liveObjects = _stillObjects = null;
    _labels = null;
    _codes = null;
    _faces = null;
    _translator = null;
  }
}

final _japaneseChars = RegExp(r'[぀-ヿ一-鿿]');
bool hasJapanese(String text) => _japaneseChars.hasMatch(text);

/// 翻訳のモデル(日本語・英語)が端末に入っているか。
Future<bool> translationReady() async {
  final manager = OnDeviceTranslatorModelManager();
  return await manager.isModelDownloaded(TranslateLanguage.japanese.bcpCode) &&
      await manager.isModelDownloaded(TranslateLanguage.english.bcpCode);
}

/// 翻訳のモデルを端末に入れる(Google のサーバーから。日本語と英語で合わせて約 60MB)。
Future<bool> downloadTranslation({required bool wifiOnly}) async {
  final manager = OnDeviceTranslatorModelManager();
  final ja = await manager.downloadModel(TranslateLanguage.japanese.bcpCode, isWifiRequired: wifiOnly);
  final en = await manager.downloadModel(TranslateLanguage.english.bcpCode, isWifiRequired: wifiOnly);
  return ja && en;
}

/// カメラの映像の1コマを、ML Kit に渡せる形にする(Android は NV21)。向きを直した画像の大きさも返す。
(InputImage, Size)? frameToInput(CameraImage image, CameraDescription camera) {
  final rotation = InputImageRotationValue.fromRawValue(camera.sensorOrientation);
  if (rotation == null) return null;
  final format = InputImageFormatValue.fromRawValue(image.format.raw as int);
  if (format == null || (Platform.isAndroid && format != InputImageFormat.nv21) || image.planes.length != 1) return null;
  final plane = image.planes.first;
  final size = Size(image.width.toDouble(), image.height.toDouble());
  final input = InputImage.fromBytes(
      bytes: plane.bytes, metadata: InputImageMetadata(size: size, rotation: rotation, format: format, bytesPerRow: plane.bytesPerRow));
  final sideways = rotation == InputImageRotation.rotation90deg || rotation == InputImageRotation.rotation270deg;
  return (input, sideways ? Size(size.height, size.width) : size);
}

@visibleForTesting
String debugDescribe(Seen seen) => seen.describe();
