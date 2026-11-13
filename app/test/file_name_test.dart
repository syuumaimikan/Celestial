import 'package:celestial/core/models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('保存するファイルの名前は PC と同じ決まりで作る', () {
    // 途中で切られた題名は、読点などの切れ目まで戻す
    expect(fileNameFrom('作業フォルダに、4月 1,200,000円、5月 1,350,000円、6月 1…'), '作業フォルダに、4月 1,200,000円、5月 1,350,000円');
    expect(fileNameFrom('売上の報告'), '売上の報告');
    expect(fileNameFrom('a/b:c*?"d'), 'a b c d'); // ファイル名に使えない文字を除く
    expect(fileNameFrom('…'), '回答');
    expect(fileNameFrom(''), '回答');
    expect(fileNameFrom('Please write a very long English title that goes on and on…'), 'Please write a very long English title');
  });
}
