# wiener-linien-epaper
Real-time Wiener Linien departure monitor for Raspberry Pi with a Waveshare 2.9″ e-Paper V2 display (epd2in9_V2), featuring departure countdowns, service alerts, and offline caching.

The 296×128 monochrome layout uses the bundled Tiny5 pixel font. Keep `assets/fonts/` alongside the Python script when deploying; no system font installation is needed. Icons are drawn directly in pixels; service disruptions appear in a two-line inverted alert strip.

Tiny5 is by The Tiny5 Project Authors, distributed under the [SIL Open Font License](assets/fonts/OFL.txt). The unmodified font is sourced from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/tiny5).
