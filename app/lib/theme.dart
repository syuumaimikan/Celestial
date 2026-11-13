import 'dart:io';

import 'package:flutter/material.dart';

/// Windows 版で使う日本語の書体(Windows 10 以降に標準で入っている)。
const windowsFont = 'Yu Gothic UI';

/// PC の画面と同じ世界観: 夜空の紺を基調に、学習の成果だけを金色で示す。絵文字は使わない。
@immutable
class CelestialColors extends ThemeExtension<CelestialColors> {
  const CelestialColors({
    required this.sunken,
    required this.line,
    required this.muted,
    required this.accentSoft,
    required this.gold,
    required this.goldSoft,
    required this.good,
    required this.goodSoft,
    required this.bad,
    required this.badSoft,
    required this.userBubble,
  });

  final Color sunken, line, muted, accentSoft, gold, goldSoft, good, goodSoft, bad, badSoft, userBubble;

  static const dark = CelestialColors(
    sunken: Color(0xFF1A2236),
    line: Color(0xFF29324A),
    muted: Color(0xFF98A2B8),
    accentSoft: Color(0xFF1F2A52),
    gold: Color(0xFFE7B955),
    goldSoft: Color(0xFF3A2F14),
    good: Color(0xFF58C697),
    goodSoft: Color(0xFF143326),
    bad: Color(0xFFF08A78),
    badSoft: Color(0xFF3B1C17),
    userBubble: Color(0xFF233062),
  );

  static const light = CelestialColors(
    sunken: Color(0xFFEBEEF5),
    line: Color(0xFFDDE2EC),
    muted: Color(0xFF5D6679),
    accentSoft: Color(0xFFE6ECFD),
    gold: Color(0xFFA66C00),
    goldSoft: Color(0xFFFBF0D6),
    good: Color(0xFF1A7F55),
    goodSoft: Color(0xFFDFF3EA),
    bad: Color(0xFFC2412D),
    badSoft: Color(0xFFFBE5E0),
    userBubble: Color(0xFFE2E8FD),
  );

  @override
  CelestialColors copyWith() => this;

  @override
  CelestialColors lerp(ThemeExtension<CelestialColors>? other, double t) {
    if (other is! CelestialColors) return this;
    Color l(Color a, Color b) => Color.lerp(a, b, t)!;
    return CelestialColors(
      sunken: l(sunken, other.sunken),
      line: l(line, other.line),
      muted: l(muted, other.muted),
      accentSoft: l(accentSoft, other.accentSoft),
      gold: l(gold, other.gold),
      goldSoft: l(goldSoft, other.goldSoft),
      good: l(good, other.good),
      goodSoft: l(goodSoft, other.goodSoft),
      bad: l(bad, other.bad),
      badSoft: l(badSoft, other.badSoft),
      userBubble: l(userBubble, other.userBubble),
    );
  }
}

extension CelestialTheme on BuildContext {
  CelestialColors get palette => Theme.of(this).extension<CelestialColors>()!;
  ColorScheme get colors => Theme.of(this).colorScheme;
}

ThemeData buildTheme(Brightness brightness) {
  final dark = brightness == Brightness.dark;
  final palette = dark ? CelestialColors.dark : CelestialColors.light;
  final scheme = ColorScheme(
    brightness: brightness,
    primary: dark ? const Color(0xFF8FA8FF) : const Color(0xFF2F55D4),
    onPrimary: dark ? const Color(0xFF0B1026) : Colors.white,
    primaryContainer: palette.accentSoft,
    onPrimaryContainer: dark ? const Color(0xFFDCE3FF) : const Color(0xFF14307F),
    secondary: palette.gold,
    onSecondary: dark ? const Color(0xFF241A00) : Colors.white,
    // 塗りつぶしの控えめなボタン(tonal)は、金色ではなく青の系統にそろえる(金色は学習の成果だけに使う)
    secondaryContainer: palette.accentSoft,
    onSecondaryContainer: dark ? const Color(0xFFDCE3FF) : const Color(0xFF14307F),
    error: palette.bad,
    onError: Colors.white,
    surface: dark ? const Color(0xFF0D1220) : const Color(0xFFF6F7FB),
    onSurface: dark ? const Color(0xFFE7EAF3) : const Color(0xFF151B2B),
    onSurfaceVariant: palette.muted,
    surfaceContainerLowest: dark ? const Color(0xFF090D17) : Colors.white,
    surfaceContainerLow: dark ? const Color(0xFF111728) : const Color(0xFFFFFFFF),
    surfaceContainer: dark ? const Color(0xFF151B2E) : const Color(0xFFF0F2F8),
    surfaceContainerHigh: dark ? const Color(0xFF1A2135) : const Color(0xFFE9ECF4),
    surfaceContainerHighest: dark ? const Color(0xFF20283E) : const Color(0xFFE2E6EF),
    outline: palette.line,
    outlineVariant: palette.line,
  );
  // Windows では日本語の書体(游ゴシック UI)を明示する。指定しないと、漢字が中国語の字形で表示されることがある
  final base = ThemeData(useMaterial3: true, colorScheme: scheme, brightness: brightness, extensions: [palette],
      fontFamily: Platform.isWindows ? windowsFont : null, fontFamilyFallback: Platform.isWindows ? const ['Meiryo UI', 'Meiryo'] : null);
  final text = base.textTheme.apply(bodyColor: scheme.onSurface, displayColor: scheme.onSurface);
  return base.copyWith(
    scaffoldBackgroundColor: scheme.surface,
    textTheme: text.copyWith(
      titleLarge: text.titleLarge?.copyWith(fontWeight: FontWeight.w700, letterSpacing: 0.2),
      titleMedium: text.titleMedium?.copyWith(fontWeight: FontWeight.w600),
      bodyMedium: text.bodyMedium?.copyWith(height: 1.55),
      bodyLarge: text.bodyLarge?.copyWith(height: 1.6),
    ),
    appBarTheme: AppBarTheme(
      backgroundColor: scheme.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: false,
      titleTextStyle: text.titleMedium?.copyWith(fontWeight: FontWeight.w700, fontSize: 17),
    ),
    cardTheme: CardThemeData(
      color: scheme.surfaceContainerLow,
      elevation: 0,
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16), side: BorderSide(color: palette.line)),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: scheme.surfaceContainerLow,
      indicatorColor: palette.accentSoft,
      surfaceTintColor: Colors.transparent,
      height: 68,
      labelTextStyle: WidgetStateProperty.resolveWith((states) => text.labelSmall?.copyWith(
            fontWeight: FontWeight.w600,
            color: states.contains(WidgetState.selected) ? scheme.primary : palette.muted,
          )),
      iconTheme: WidgetStateProperty.resolveWith((states) => IconThemeData(
            color: states.contains(WidgetState.selected) ? scheme.primary : palette.muted,
          )),
    ),
    segmentedButtonTheme: SegmentedButtonThemeData(
      style: ButtonStyle(
        backgroundColor: WidgetStateProperty.resolveWith((states) => states.contains(WidgetState.selected) ? palette.accentSoft : Colors.transparent),
        foregroundColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.disabled)) return palette.muted.withValues(alpha: 0.45);
          return states.contains(WidgetState.selected) ? scheme.primary : palette.muted;
        }),
        side: WidgetStateProperty.all(BorderSide(color: palette.line)),
      ),
    ),
    navigationRailTheme: NavigationRailThemeData(
      backgroundColor: dark ? const Color(0xFF0A0E19) : const Color(0xFF17203F),
      indicatorColor: dark ? const Color(0xFF1C2440) : const Color(0xFF2A3766),
      selectedIconTheme: const IconThemeData(color: Color(0xFFE8ECF8)),
      unselectedIconTheme: const IconThemeData(color: Color(0xFF9AA5C6)),
      // 文字の指定は本文の書体を引き継ぐ(書体名がないと、Windows で日本語が別の書体になる)
      selectedLabelTextStyle: text.labelMedium?.copyWith(color: const Color(0xFFE8ECF8), fontWeight: FontWeight.w600),
      unselectedLabelTextStyle: text.labelMedium?.copyWith(color: const Color(0xFF9AA5C6)),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: scheme.surfaceContainerLowest,
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: palette.line)),
      enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: palette.line)),
      focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: scheme.primary, width: 1.4)),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 12),
        textStyle: text.labelLarge?.copyWith(fontWeight: FontWeight.w600),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        side: BorderSide(color: palette.line),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      ),
    ),
    chipTheme: ChipThemeData(
      backgroundColor: scheme.surfaceContainerLow,
      selectedColor: palette.accentSoft,
      side: BorderSide(color: palette.line),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(999)),
      labelStyle: text.labelMedium,
      showCheckmark: false,
    ),
    dividerTheme: DividerThemeData(color: palette.line, thickness: 1, space: 1),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: scheme.onSurface,
      contentTextStyle: text.bodyMedium?.copyWith(color: scheme.surface, height: 1.4),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ),
    listTileTheme: ListTileThemeData(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12))),
    drawerTheme: DrawerThemeData(backgroundColor: scheme.surfaceContainerLow, surfaceTintColor: Colors.transparent),
  );
}
