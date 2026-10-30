// The TV's screen: five channels, CRT power on/off, static between channels, and an explosion.
//
// Everything is drawn into one full-screen 16-bit sprite (240 x 135, 64.8 KB, allocated in begin()) and
// pushed once per frame, so nothing flickers. The sprite stores its pixels byte-swapped (TFT_eSPI's
// SPI order): the effects that write the buffer directly use pre-swapped colours.
//
// Channels:
//   1 TEST CARD   colour bars, a test circle with a sweeping beam, and the uptime clock
//   2 SPACE       a 3D starfield at warp speed past a ringed planet
//   3 PLASMA      demoscene plasma (four sine fields, a rotating palette)
//   4 FIREPLACE   demoscene fire burning on two logs
//   5 ULTRASONIC  the detector live: both bands' SNR scrolling, the threshold, each strike marked
#pragma once

#include <Arduino.h>
#include <TFT_eSPI.h>

namespace tvscreen {

const int kChannels = 5;

// What the ULTRASONIC channel shows, updated by loop().
struct Live {
  float snrDb[2];       // the bands' SNR in the last block
  bool learning;
  float thresholdDb;
  float centerHz[2];
  uint32_t hits[3];     // decided strikes: 38, 47, both
  float rmsDbfs;
};

class Screen {
 public:
  explicit Screen(TFT_eSPI &tft) : tft_(tft), spr_(&tft) {}

  // Allocates the sprite and sets up the backlight PWM. False if the sprite does not fit.
  bool begin();
  // The boot splash (blocking, ~1.5 s), then standby.
  void splash();

  // The TV state changed (power, channel) or exploded: starts the matching animation.
  void powerOn(int channel, uint32_t now);
  void powerOff(uint32_t now);
  void changeChannel(int channel, uint32_t now);
  void boom(uint32_t now);
  // A decided strike, for the ULTRASONIC channel's marks: 0 = 38, 1 = 47, 2 = both.
  void markStrike(int kind);
  // Samples the live levels (every call; the scope keeps one sample every 50 ms).
  void sample(const Live &live, uint32_t now);

  // Draws and pushes a frame when one is due. Returns quickly otherwise.
  void frame(uint32_t now);

  bool on() const { return on_; }
  int channel() const { return channel_; }
  const char *channelName(int channel) const;
  // Writes the current frame as hex rows on Serial ("SHOT_ROW y hex"), paced for the USB/IP link.
  void dump(int y0, int y1);

 private:
  enum class Anim : uint8_t { None, PowerOn, PowerOff, Switch, Boom };

  void drawChannel(int channel, uint32_t now);
  void drawTestCard(uint32_t now);
  void drawSpace(uint32_t now);
  void drawPlasma(uint32_t now);
  void drawFire();
  void drawScope();
  void drawOsd(uint32_t now);
  void drawStatic();
  void drawBoom(float t);
  void drawStandby();
  void backlight(int level);  // 0-255
  uint32_t rnd();

  TFT_eSPI &tft_;
  TFT_eSprite spr_;
  uint16_t *buf_ = nullptr;  // the sprite's pixels (byte-swapped)
  bool on_ = false;
  int channel_ = 0;
  Anim anim_ = Anim::None;
  uint32_t animStart_ = 0;
  uint32_t osdUntil_ = 0;
  uint32_t lastFrame_ = 0;
  uint32_t rng_ = 0x12345678u;
  bool standbyDrawn_ = false;
  float lastT_ = 0;  // seconds, for the effects' dt

  // SPACE
  static const int kStars = 160;
  float starX_[kStars], starY_[kStars], starZ_[kStars];
  // PLASMA
  uint8_t sine_[256];
  uint16_t palette_[256];  // byte-swapped
  // FIREPLACE (third resolution: 3 x 3 pixels per cell; 2 more rows than shown seed the flames)
  static const int kFireW = 80, kFireH = 47;
  uint8_t *heat_ = nullptr;
  uint16_t firePalette_[256];  // byte-swapped
  // ULTRASONIC
  static const int kScope = 240;
  uint8_t scopeA_[kScope], scopeB_[kScope];  // SNR in dB, 0-70
  uint8_t scopeMark_[kScope];                // 0 none, 1 = 38, 2 = 47, 3 = both
  int scopeHead_ = 0;
  uint32_t lastSample_ = 0;
  uint8_t pendingMark_ = 0;
  Live live_ = {};
  // BOOM
  static const int kParticles = 140;
  float px_[kParticles], py_[kParticles], pvx_[kParticles], pvy_[kParticles];
  uint8_t pkind_[kParticles];
};

}  // namespace tvscreen
