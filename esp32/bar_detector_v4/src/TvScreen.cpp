#include "TvScreen.h"

#include <math.h>

namespace tvscreen {

namespace {

const int W = 240, H = 135;
const int CX = W / 2, CY = H / 2;
const int BL_CHANNEL = 0;       // LEDC channel of the backlight
const int BL_STANDBY = 18;      // the dim glow of a TV in standby
const uint32_t FRAME_MS = 30;   // ~33 frames/s at most
const uint32_t POWER_ON_MS = 450;
const uint32_t POWER_OFF_MS = 650;
const uint32_t SWITCH_MS = 320;
const float BOOM_S = 1.9f;
const uint32_t OSD_MS = 2500;
const float PI_F = 3.14159265f;

const char *NAMES[kChannels] = {"TEST CARD", "SPACE", "PLASMA", "FIREPLACE", "ULTRASONIC"};

inline uint16_t rgb(int r, int g, int b) {
  r = r < 0 ? 0 : (r > 255 ? 255 : r);
  g = g < 0 ? 0 : (g > 255 ? 255 : g);
  b = b < 0 ? 0 : (b > 255 ? 255 : b);
  return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}
inline uint16_t swap16(uint16_t c) { return (uint16_t)((c >> 8) | (c << 8)); }
inline float clamp01(float v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
inline float easeOut(float p) { return 1 - (1 - p) * (1 - p); }

const uint16_t OSD_GREEN = rgb(70, 255, 90);
const uint16_t SKY = rgb(90, 190, 255);
const uint16_t ORANGE = rgb(255, 150, 30);

}  // namespace

uint32_t Screen::rnd() {
  rng_ ^= rng_ << 13;
  rng_ ^= rng_ >> 17;
  rng_ ^= rng_ << 5;
  return rng_;
}

void Screen::backlight(int level) { ledcWrite(BL_CHANNEL, level); }

const char *Screen::channelName(int channel) const { return NAMES[channel % kChannels]; }

bool Screen::begin() {
  // The sprite first: it needs 64.8 KB in one piece, before the strike spectrum fragments the heap.
  spr_.setColorDepth(16);
  buf_ = (uint16_t *)spr_.createSprite(W, H);
  if (!buf_) return false;
  heat_ = (uint8_t *)calloc(kFireW * kFireH, 1);
  if (!heat_) return false;
  for (int i = 0; i < 256; i++) {
    sine_[i] = (uint8_t)(128 + 127 * sinf(2 * PI_F * i / 256));
    // Fire: black, red, orange, yellow, white.
    int r = i * 3, g = (i - 80) * 3, b = (i - 170) * 4;
    firePalette_[i] = swap16(rgb(r, g, b));
  }
  for (int i = 0; i < kStars; i++) {
    starX_[i] = ((int)(rnd() % 2001) - 1000) / 1000.0f;
    starY_[i] = ((int)(rnd() % 2001) - 1000) / 1000.0f;
    starZ_[i] = 0.05f + (rnd() % 950) / 1000.0f;
  }
  for (int i = 0; i < kScope; i++) scopeA_[i] = scopeB_[i] = scopeMark_[i] = 0;
  ledcSetup(BL_CHANNEL, 5000, 8);
  ledcAttachPin(TFT_BL, BL_CHANNEL);
  backlight(255);
  return true;
}

void Screen::splash() {
  backlight(0);
  spr_.fillSprite(TFT_BLACK);
  // Three "bars" as a logo, ringing.
  for (int i = 0; i < 3; i++) {
    int x = 92 + i * 22, h = 34 - i * 6;
    spr_.fillRoundRect(x, 38 - h / 2, 12, h, 4, i == 1 ? ORANGE : SKY);
    for (int k = 1; k <= 3; k++) spr_.drawCircle(x + 6, 38, 10 + k * 7 + i * 3, rgb(30, 60 + 30 * k, 90 + 40 * k));
  }
  spr_.fillRoundRect(86, 18, 68, 40, 6, TFT_BLACK);
  spr_.fillRoundRect(92, 30, 12, 22, 4, SKY);
  spr_.fillRoundRect(114, 24, 12, 34, 4, ORANGE);
  spr_.fillRoundRect(136, 34, 12, 14, 4, SKY);
  spr_.setTextDatum(MC_DATUM);
  spr_.setTextColor(TFT_WHITE);
  spr_.drawString("SPACE COMMAND", CX, 76, 4);
  spr_.setTextColor(rgb(150, 150, 150));
  spr_.drawString("ultrasonic television", CX, 98, 2);
  spr_.setTextColor(SKY);
  spr_.drawString("38 kHz  power", 62, 120, 2);
  spr_.setTextColor(ORANGE);
  spr_.drawString("47 kHz  channel", 178, 120, 2);
  spr_.pushSprite(0, 0);
  for (int b = 0; b <= 255; b += 15) {
    backlight(b);
    delay(20);
  }
  delay(1300);
  standbyDrawn_ = false;
  powerOff(millis());
}

void Screen::powerOn(int channel, uint32_t now) {
  on_ = true;
  channel_ = channel % kChannels;
  anim_ = Anim::PowerOn;
  animStart_ = now;
  osdUntil_ = now + POWER_ON_MS + OSD_MS;
  standbyDrawn_ = false;
  backlight(255);
}

void Screen::powerOff(uint32_t now) {
  on_ = false;
  anim_ = Anim::PowerOff;
  animStart_ = now;
  standbyDrawn_ = false;
}

void Screen::changeChannel(int channel, uint32_t now) {
  channel_ = channel % kChannels;
  anim_ = Anim::Switch;
  animStart_ = now;
  osdUntil_ = now + SWITCH_MS + OSD_MS;
}

void Screen::boom(uint32_t now) {
  anim_ = Anim::Boom;
  animStart_ = now;
  standbyDrawn_ = false;
  backlight(255);
  for (int i = 0; i < kParticles; i++) {
    float a = (rnd() % 6283) / 1000.0f;
    float v = 50 + rnd() % 260;
    px_[i] = CX;
    py_[i] = CY;
    pvx_[i] = v * cosf(a);
    pvy_[i] = v * sinf(a) * 0.8f - 40;
    pkind_[i] = rnd() % 3;  // 0 spark, 1 ember, 2 debris
  }
  lastT_ = 0;
}

void Screen::markStrike(int kind) { pendingMark_ = (uint8_t)(kind + 1); }

void Screen::sample(const Live &live, uint32_t now) {
  live_ = live;
  if (now - lastSample_ < 50) return;
  lastSample_ = now;
  auto db = [](float v) -> uint8_t { return (uint8_t)(v < 0 ? 0 : (v > 70 ? 70 : v)); };
  scopeA_[scopeHead_] = live.learning ? 0 : db(live.snrDb[0]);
  scopeB_[scopeHead_] = live.learning ? 0 : db(live.snrDb[1]);
  scopeMark_[scopeHead_] = pendingMark_;
  pendingMark_ = 0;
  scopeHead_ = (scopeHead_ + 1) % kScope;
}

// ---------- the channels ----------

void Screen::drawTestCard(uint32_t now) {
  static const uint8_t bars[7][3] = {{192, 192, 192}, {192, 192, 0}, {0, 192, 192}, {0, 192, 0},
                                     {192, 0, 192},   {192, 0, 0},   {0, 0, 192}};
  static const uint8_t strip[7][3] = {{0, 0, 192}, {16, 16, 16}, {192, 0, 192}, {16, 16, 16},
                                      {0, 192, 192}, {16, 16, 16}, {192, 192, 192}};
  for (int i = 0; i < 7; i++) {
    int x0 = i * W / 7, x1 = (i + 1) * W / 7;
    spr_.fillRect(x0, 0, x1 - x0, 88, rgb(bars[i][0], bars[i][1], bars[i][2]));
    spr_.fillRect(x0, 88, x1 - x0, 10, rgb(strip[i][0], strip[i][1], strip[i][2]));
  }
  spr_.fillRect(0, 98, W, H - 98, TFT_BLACK);
  spr_.fillRect(0, 98, 40, 37, rgb(0, 33, 76));
  spr_.fillRect(40, 98, 40, 37, TFT_WHITE);
  spr_.fillRect(80, 98, 40, 37, rgb(50, 0, 106));
  // The test circle, its cross, and a radar beam sweeping once every 3 s.
  spr_.fillCircle(CX, 54, 42, TFT_BLACK);
  float a = (now % 3000) / 3000.0f * 2 * PI_F;
  for (int k = 12; k >= 1; k--) {
    float b0 = a - k * 0.07f, b1 = a - (k - 1) * 0.07f;
    int g = 255 - k * 19;
    spr_.fillTriangle(CX, 54, CX + (int)(41 * cosf(b0)), 54 + (int)(41 * sinf(b0)), CX + (int)(41 * cosf(b1)),
                      54 + (int)(41 * sinf(b1)), rgb(0, g, g / 4));
  }
  spr_.drawLine(CX, 54, CX + (int)(41 * cosf(a)), 54 + (int)(41 * sinf(a)), rgb(180, 255, 180));
  spr_.drawCircle(CX, 54, 42, TFT_WHITE);
  spr_.drawCircle(CX, 54, 43, TFT_WHITE);
  spr_.drawCircle(CX, 54, 28, rgb(90, 90, 90));
  spr_.drawFastHLine(CX - 42, 54, 84, TFT_WHITE);
  spr_.drawFastVLine(CX, 12, 84, TFT_WHITE);
  spr_.setTextDatum(MC_DATUM);
  spr_.setTextColor(TFT_WHITE);
  spr_.drawString("SPACE COMMAND", 180, 108, 2);
  // Uptime clock.
  char text[16];
  uint32_t s = now / 1000;
  snprintf(text, sizeof(text), "%02lu:%02lu:%02lu", (unsigned long)(s / 3600), (unsigned long)(s / 60 % 60),
           (unsigned long)(s % 60));
  spr_.setTextColor(TFT_YELLOW);
  spr_.drawString(text, 180, 125, 2);
  if ((now / 500) % 2) spr_.fillCircle(232, 8, 3, TFT_RED);
}

void Screen::drawSpace(uint32_t now) {
  float t = now / 1000.0f;
  float dt = t - lastT_;
  if (dt < 0 || dt > 0.1f) dt = 0.03f;
  lastT_ = t;
  spr_.fillSprite(TFT_BLACK);
  // A ringed planet, lit from the top left.
  const int px = 196, py = 104;
  spr_.drawEllipse(px, py, 50, 9, rgb(120, 100, 70));
  for (int r = 30; r > 0; r -= 2) {
    float k = 1 - r / 30.0f;
    spr_.fillCircle(px - (30 - r) / 3, py - (30 - r) / 3, r, rgb(110 + 120 * k, 60 + 110 * k, 40 + 60 * k));
  }
  spr_.drawEllipse(px, py, 46, 7, rgb(200, 170, 120));
  spr_.drawEllipse(px, py, 43, 6, rgb(160, 130, 90));
  // Warp: the speed swells every ~15 s.
  float speed = 0.35f + 0.3f * sinf(t * 0.4f) + 0.3f;
  for (int i = 0; i < kStars; i++) {
    float z0 = starZ_[i];
    starZ_[i] -= speed * dt;
    float z = starZ_[i];
    int sx = CX + (int)(starX_[i] / z * 70), sy = CY + (int)(starY_[i] / z * 70);
    if (z < 0.03f || sx < 0 || sx >= W || sy < 0 || sy >= H) {
      starX_[i] = ((int)(rnd() % 2001) - 1000) / 1000.0f;
      starY_[i] = ((int)(rnd() % 2001) - 1000) / 1000.0f;
      starZ_[i] = 1.0f;
      continue;
    }
    float zt = z0 + speed * 0.18f;  // the trail: where the star was ~0.2 s ago
    int tx = CX + (int)(starX_[i] / zt * 70), ty = CY + (int)(starY_[i] / zt * 70);
    float b = clamp01(1.3f - z);
    uint16_t c = rgb((int)(220 * b), (int)(230 * b), (int)(255 * clamp01(b + 0.35f)));
    spr_.drawLine(tx, ty, sx, sy, c);
    if (z < 0.25f) spr_.fillRect(sx, sy, 2, 2, TFT_WHITE);
  }
  spr_.setTextDatum(TC_DATUM);
  float pulse = 0.6f + 0.4f * sinf(t * 2);
  spr_.setTextColor(rgb((int)(80 * pulse), (int)(220 * pulse), (int)(255 * pulse)));
  spr_.drawString("S P A C E   C O M M A N D", CX, 4, 2);
}

void Screen::drawPlasma(uint32_t now) {
  float t = now / 1000.0f;
  for (int i = 0; i < 256; i++) {
    float p = PI_F * i / 128;
    palette_[i] = swap16(rgb((int)(128 + 127 * sinf(p + t * 0.7f)), (int)(128 + 127 * sinf(p + 2.1f + t * 0.5f)),
                             (int)(128 + 127 * sinf(p + 4.2f + t * 0.3f))));
  }
  const int a = (int)(t * 40), b = (int)(t * -31), c = (int)(t * 23), d = (int)(t * -47);
  const float cx = 60 + 40 * sinf(t * 0.5f), cy = 34 + 25 * cosf(t * 0.6f);
  for (int y = 0; y < 68; y++) {
    uint16_t *row0 = buf_ + (2 * y) * W;
    uint16_t *row1 = 2 * y + 1 < H ? row0 + W : nullptr;
    const float dy = y - cy;
    for (int x = 0; x < 120; x++) {
      const float dx = x - cx;
      const int dist = (int)(sqrtf(dx * dx + dy * dy) * 4);
      const int v = sine_[(x * 3 + a) & 255] + sine_[(y * 5 + b) & 255] + sine_[((x + y) * 2 + c) & 255] +
                    sine_[(dist + d) & 255];
      const uint16_t col = palette_[(v >> 2) & 255];
      row0[2 * x] = row0[2 * x + 1] = col;
      if (row1) row1[2 * x] = row1[2 * x + 1] = col;
    }
  }
}

void Screen::drawFire() {
  uint8_t *h = heat_;
  // The embers under the logs (cells 15-65 of 80) flicker at full heat.
  uint8_t *bottom = h + (kFireH - 1) * kFireW;
  for (int x = 0; x < kFireW; x++) {
    bool lit = x > 15 && x < 65;
    bottom[x] = lit ? ((rnd() & 0xFF) > 40 ? 255 : 150) : 0;
  }
  // Each cell rises one row, drifting sideways and cooling a little.
  for (int y = 1; y < kFireH; y++) {
    for (int x = 0; x < kFireW; x++) {
      const uint32_t r = rnd();
      const int src = h[y * kFireW + x];
      int dst = x - (int)(r & 3) + 1;
      if (dst < 0) dst = 0;
      if (dst >= kFireW) dst = kFireW - 1;
      const int v = src - (int)((r >> 2) & 7);
      h[(y - 1) * kFireW + dst] = (uint8_t)(v < 0 ? 0 : v);
    }
  }
  for (int y = 0; y < H / 3; y++) {
    uint16_t *row = buf_ + (3 * y) * W;
    for (int x = 0; x < kFireW; x++) {
      const uint16_t col = firePalette_[h[y * kFireW + x]];
      for (int dy = 0; dy < 3; dy++) {
        uint16_t *p = row + dy * W + 3 * x;
        p[0] = p[1] = p[2] = col;
      }
    }
  }
  // Two logs.
  spr_.fillRoundRect(36, 121, 96, 12, 5, rgb(90, 50, 25));
  spr_.fillRoundRect(110, 123, 96, 11, 5, rgb(70, 38, 18));
  spr_.drawFastHLine(44, 125, 80, rgb(140, 70, 20));
  spr_.drawFastHLine(118, 127, 80, rgb(120, 55, 15));
}

void Screen::drawScope() {
  spr_.fillSprite(rgb(0, 10, 0));
  auto yOf = [](int db) { return 122 - db * 3 / 2; };
  for (int db = 0; db <= 70; db += 10) spr_.drawFastHLine(0, yOf(db), W, rgb(0, db == 0 ? 90 : 45, 0));
  for (int x = 0; x < W; x += 40) spr_.drawFastVLine(x, 16, 106, rgb(0, 45, 0));
  // Threshold, dashed.
  int ty = yOf((int)live_.thresholdDb);
  for (int x = 0; x < W; x += 8) spr_.drawFastHLine(x, ty, 4, rgb(200, 200, 200));
  // Strike marks, then the traces (oldest on the left).
  spr_.setTextDatum(TC_DATUM);
  for (int i = 0; i < kScope; i++) {
    int k = (scopeHead_ + i) % kScope;
    if (!scopeMark_[k]) continue;
    uint16_t c = scopeMark_[k] == 1 ? SKY : (scopeMark_[k] == 2 ? ORANGE : TFT_RED);
    spr_.drawFastVLine(i, 16, 106, c);
    spr_.setTextColor(c);
    spr_.drawString(scopeMark_[k] == 1 ? "38" : (scopeMark_[k] == 2 ? "47" : "!!"), i < 12 ? 12 : i, 17, 1);
  }
  for (int i = 1; i < kScope; i++) {
    int k0 = (scopeHead_ + i - 1) % kScope, k1 = (scopeHead_ + i) % kScope;
    spr_.drawLine(i - 1, yOf(scopeA_[k0]), i, yOf(scopeA_[k1]), SKY);
    spr_.drawLine(i - 1, yOf(scopeB_[k0]), i, yOf(scopeB_[k1]), ORANGE);
  }
  // Header and footer.
  char text[48];
  spr_.fillRect(0, 0, W, 14, TFT_BLACK);
  spr_.setTextDatum(TL_DATUM);
  spr_.setTextColor(SKY);
  snprintf(text, sizeof(text), "%.2fk %2.0fdB", live_.centerHz[0] / 1000, live_.learning ? 0.0f : fmaxf(0.0f, live_.snrDb[0]));
  spr_.drawString(text, 2, 0, 2);
  spr_.setTextDatum(TR_DATUM);
  spr_.setTextColor(ORANGE);
  snprintf(text, sizeof(text), "%.2fk %2.0fdB", live_.centerHz[1] / 1000, live_.learning ? 0.0f : fmaxf(0.0f, live_.snrDb[1]));
  spr_.drawString(text, W - 2, 0, 2);
  spr_.fillRect(0, 124, W, 11, TFT_BLACK);
  spr_.setTextDatum(TL_DATUM);
  spr_.setTextColor(rgb(160, 160, 160));
  snprintf(text, sizeof(text), "38k:%lu  47k:%lu  both:%lu   mic %.0f dBFS", (unsigned long)live_.hits[0],
           (unsigned long)live_.hits[1], (unsigned long)live_.hits[2], live_.rmsDbfs);
  spr_.drawString(text, 2, 126, 1);
  if (live_.learning) {
    spr_.setTextDatum(MC_DATUM);
    spr_.setTextColor(TFT_YELLOW);
    spr_.drawString("LEARNING - keep quiet", CX, CY, 2);
  }
}

void Screen::drawChannel(int channel, uint32_t now) {
  switch (channel) {
    case 0:
      drawTestCard(now);
      break;
    case 1:
      drawSpace(now);
      break;
    case 2:
      drawPlasma(now);
      break;
    case 3:
      drawFire();
      break;
    default:
      drawScope();
      break;
  }
}

void Screen::drawOsd(uint32_t now) {
  if ((int32_t)(osdUntil_ - now) <= 0) return;
  char text[8];
  snprintf(text, sizeof(text), "%d", channel_ + 1);
  spr_.fillRoundRect(4, 4, 34, 34, 5, TFT_BLACK);
  spr_.setTextDatum(MC_DATUM);
  spr_.setTextColor(OSD_GREEN);
  spr_.drawString(text, 21, 22, 4);
  int w = spr_.textWidth(NAMES[channel_], 2) + 10;
  spr_.fillRoundRect(42, 13, w, 18, 4, TFT_BLACK);
  spr_.setTextDatum(ML_DATUM);
  spr_.drawString(NAMES[channel_], 47, 22, 2);
}

// TV snow, with a darker band rolling down.
void Screen::drawStatic() {
  const int roll = (int)((millis() / 4) % (H + 40)) - 20;
  for (int y = 0; y < H; y++) {
    uint16_t *row = buf_ + y * W;
    const int dark = (y > roll && y < roll + 18) ? 2 : 0;
    for (int x = 0; x < W; x += 2) {
      uint32_t r = rnd();
      int g0 = (r & 0xFF) >> dark, g1 = ((r >> 8) & 0xFF) >> dark;
      row[x] = swap16(rgb(g0, g0, g0));
      row[x + 1] = swap16(rgb(g1, g1, g1));
    }
  }
}

void Screen::drawBoom(float t) {
  float dt = t - lastT_;
  if (dt < 0 || dt > 0.1f) dt = 0.03f;
  lastT_ = t;
  // Flash: white, then a hot orange that cools to black.
  if (t < 0.06f) {
    spr_.fillSprite(TFT_WHITE);
  } else if (t < 0.35f) {
    float k = (t - 0.06f) / 0.29f;
    spr_.fillSprite(rgb((int)(255 * (1 - k)), (int)(170 * (1 - k) * (1 - k)), (int)(40 * (1 - k))));
  } else {
    spr_.fillSprite(TFT_BLACK);
  }
  // The screen shakes for 0.8 s.
  int amp = t < 0.8f ? (int)(7 * (1 - t / 0.8f)) : 0;
  int ox = amp ? (int)(rnd() % (2 * amp + 1)) - amp : 0, oy = amp ? (int)(rnd() % (2 * amp + 1)) - amp : 0;
  const int cx = CX + ox, cy = CY + oy;
  // Smoke clouds, rising and fading.
  if (t > 0.35f) {
    float k = (t - 0.35f) / (BOOM_S - 0.35f);
    int g = (int)(70 * (1 - k));
    static const int8_t sx[6] = {-30, 25, -10, 40, -45, 10}, sy[6] = {-5, -12, -25, 5, 8, 15};
    for (int i = 0; i < 6; i++)
      spr_.fillCircle(cx + sx[i] * (1 + k), cy + sy[i] - (int)(30 * k), 12 + (int)(28 * k), rgb(g, g, g));
  }
  // Fireball: layers that swell, then shrink.
  if (t < 0.7f) {
    float k = t / 0.7f;
    float r = 58 * sqrtf(k) * (1 - k * k);
    if (r > 1) {
      spr_.fillCircle(cx, cy, (int)r, rgb(200, 30, 0));
      spr_.fillCircle(cx, cy, (int)(r * 0.75f), rgb(255, 120, 0));
      spr_.fillCircle(cx, cy, (int)(r * 0.5f), rgb(255, 220, 60));
      spr_.fillCircle(cx, cy, (int)(r * 0.25f), TFT_WHITE);
    }
  }
  // Shockwave ring.
  if (t < 0.9f) {
    float k = t / 0.9f;
    int r = 12 + (int)(200 * sqrtf(k));
    int b = (int)(255 * (1 - k));
    for (int w = 0; w < 3; w++) spr_.drawCircle(cx, cy, r - w, rgb(b, (int)(b * 0.8f), (int)(b * 0.5f)));
  }
  // Sparks, embers and debris.
  for (int i = 0; i < kParticles; i++) {
    const float drag = pkind_[i] == 0 ? 0.985f : 0.97f;
    pvx_[i] *= drag;
    pvy_[i] = pvy_[i] * drag + (pkind_[i] == 2 ? 260.0f : 110.0f) * dt;
    px_[i] += pvx_[i] * dt;
    py_[i] += pvy_[i] * dt;
    int x = (int)px_[i] + ox, y = (int)py_[i] + oy;
    if (x < 0 || x >= W - 2 || y < 0 || y >= H - 2) continue;
    float age = t / BOOM_S;
    uint16_t c;
    if (pkind_[i] == 2) {
      int g = (int)(110 * (1 - age));
      c = rgb(g, g * 3 / 4, g / 2);
    } else if (age < 0.15f) {
      c = TFT_WHITE;
    } else if (age < 0.35f) {
      c = rgb(255, 230, 80);
    } else if (age < 0.6f) {
      c = rgb(255, 130, 20);
    } else {
      c = rgb((int)(255 * (1.3f - age)), 30, 0);
    }
    spr_.fillRect(x, y, pkind_[i] == 1 ? 1 : 2, pkind_[i] == 1 ? 1 : 2, c);
  }
  // BOOM!
  if (t > 0.08f && t < 1.6f) {
    int size = t < 0.2f ? 3 : 2;
    spr_.setTextDatum(MC_DATUM);
    spr_.setTextSize(size);
    spr_.setTextColor(rgb(160, 0, 0));
    for (int dx = -2; dx <= 2; dx += 2)
      for (int dy = -2; dy <= 2; dy += 2) spr_.drawString("BOOM!", cx + dx, cy + dy, 4);
    spr_.setTextColor(t < 0.2f ? TFT_WHITE : TFT_YELLOW);
    spr_.drawString("BOOM!", cx, cy, 4);
    spr_.setTextSize(1);
  }
}

void Screen::drawStandby() {
  spr_.fillSprite(TFT_BLACK);
  spr_.fillCircle(W - 8, H - 8, 2, TFT_RED);  // the standby LED
  spr_.pushSprite(0, 0);
  backlight(BL_STANDBY);
  standbyDrawn_ = true;
}

// ---------- frames ----------

void Screen::frame(uint32_t now) {
  if (!on_ && anim_ == Anim::None) {
    if (!standbyDrawn_) drawStandby();
    return;
  }
  if (now - lastFrame_ < FRAME_MS) return;
  lastFrame_ = now;
  const uint32_t el = now - animStart_;

  if (anim_ == Anim::PowerOn && el >= POWER_ON_MS) anim_ = Anim::None;
  if (anim_ == Anim::Switch && el >= SWITCH_MS) anim_ = Anim::None;
  if (anim_ == Anim::Boom && el >= (uint32_t)(BOOM_S * 1000)) {
    anim_ = Anim::None;
    if (on_) {
      osdUntil_ = now + OSD_MS;
    } else {
      powerOff(now);  // back to standby the CRT way
    }
  }
  if (anim_ == Anim::PowerOff && el >= POWER_OFF_MS) {
    anim_ = Anim::None;
    drawStandby();
    return;
  }

  switch (anim_) {
    case Anim::PowerOn: {
      // A dot grows into a line, then the line opens into the picture.
      float p = (float)el / POWER_ON_MS;
      if (p < 0.25f) {
        spr_.fillSprite(TFT_BLACK);
        int w = 4 + (int)(236 * easeOut(p / 0.25f));
        spr_.fillRect(CX - w / 2, CY - 1, w, 3, TFT_WHITE);
        spr_.drawFastHLine(CX - w / 2, CY - 2, w, rgb(120, 140, 255));
        spr_.drawFastHLine(CX - w / 2, CY + 2, w, rgb(120, 140, 255));
      } else {
        drawChannel(channel_, now);
        int h = 3 + (int)(132 * easeOut((p - 0.25f) / 0.75f));
        int top = CY - h / 2, bottom = CY + h / 2;
        if (top > 0) spr_.fillRect(0, 0, W, top, TFT_BLACK);
        if (bottom < H - 1) spr_.fillRect(0, bottom + 1, W, H - bottom - 1, TFT_BLACK);
        spr_.drawFastHLine(0, top, W, rgb(200, 210, 255));
        spr_.drawFastHLine(0, bottom, W, rgb(200, 210, 255));
      }
      break;
    }
    case Anim::PowerOff: {
      // The picture collapses to a bright line, the line to a dot, and the dot fades.
      float p = (float)el / POWER_OFF_MS;
      if (p < 0.35f) {
        float q = p / 0.35f;
        drawChannel(channel_, now);
        int h = 3 + (int)(132 * (1 - q) * (1 - q));
        int top = CY - h / 2, bottom = CY + h / 2;
        if (q > 0.6f) spr_.fillRect(0, top, W, bottom - top + 1, rgb(230, 235, 255));
        if (top > 0) spr_.fillRect(0, 0, W, top, TFT_BLACK);
        if (bottom < H - 1) spr_.fillRect(0, bottom + 1, W, H - bottom - 1, TFT_BLACK);
      } else if (p < 0.6f) {
        float q = (p - 0.35f) / 0.25f;
        spr_.fillSprite(TFT_BLACK);
        int w = 3 + (int)(237 * (1 - easeOut(q)));
        spr_.fillRect(CX - w / 2, CY - 1, w, 3, TFT_WHITE);
      } else {
        float q = (p - 0.6f) / 0.4f;
        spr_.fillSprite(TFT_BLACK);
        int g = (int)(255 * (1 - q));
        spr_.fillCircle(CX, CY, 3, rgb(g, g, g));
        spr_.fillCircle(CX, CY, 1, TFT_WHITE);
      }
      break;
    }
    case Anim::Switch:
      drawStatic();
      break;
    case Anim::Boom:
      drawBoom(el / 1000.0f);
      break;
    default:
      drawChannel(channel_, now);
      drawOsd(now);
      break;
  }
  spr_.pushSprite(0, 0);
}

void Screen::dump(int y0, int y1) {
  if (y0 < 0) y0 = 0;
  if (y1 > H - 1) y1 = H - 1;
  static char line[16 + W * 4 + 2];
  Serial.printf("SHOT w=%d h=%d y0=%d y1=%d\n", W, H, y0, y1);
  for (int y = y0; y <= y1; y++) {
    int n = snprintf(line, sizeof(line), "SHOT_ROW %d ", y);
    for (int x = 0; x < W; x++) n += snprintf(line + n, sizeof(line) - n, "%04x", swap16(buf_[y * W + x]));
    Serial.println(line);
    Serial.flush();
    delay(25);  // ~40 KB/s: large bursts drop the USB/IP link
  }
  Serial.println("SHOT end");
}

}  // namespace tvscreen
