// Life Dashboard firmware for the TRMNL 7.5" (OG) DIY Kit (XIAO ESP32-S3 Plus).
//
// Each wake: join Wi-Fi -> download screen.bin (800x480, 1 bit/pixel, 1 = black)
// -> redraw only if it changed -> deep sleep. Press button D1 to refresh now.
//
// Arduino IDE: board "XIAO_ESP32S3_PLUS", library Seeed_GFX, driver.h in this folder.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <esp_sleep.h>
#include <driver/rtc_io.h>
#include <rom/crc.h>
#include "driver.h"
#include "secrets.h"   // WIFI_SSID, WIFI_PASS, IMAGE_URL

// ---------- settings ----------
const uint32_t REFRESH_MIN       = 15;  // normal refresh interval
const uint32_t RETRY_MIN         = 5;   // after a failed download
const uint32_t NIGHT_REFRESH_MIN = 60;  // between NIGHT_START and NIGHT_END (see isNight)
const int      NIGHT_START = 23, NIGHT_END = 6;
const char*    TZ_POSIX = "CST6CDT,M3.2.0,M11.1.0";  // America/Chicago

// ---------- hardware ----------
const int W = 800, H = 480;
const size_t FRAME_BYTES = W * H / 8;   // 48,000
#define BUTTON_PIN  GPIO_NUM_2          // D1
#define BATTERY_PIN 1                   // D0 / GPIO1  (BAT_ADC)
#define ADC_EN_PIN  6                   // GPIO6       (ADC_EN)

EPaper epaper = EPaper();
RTC_DATA_ATTR uint32_t lastCrc = 0;     // survives deep sleep
RTC_DATA_ATTR uint32_t bootCount = 0;

float readBattery() {
  pinMode(ADC_EN_PIN, OUTPUT);
  digitalWrite(ADC_EN_PIN, HIGH);
  delay(10);
  analogReadResolution(12);
  analogSetPinAttenuation(BATTERY_PIN, ADC_11db);
  long sum = 0;
  for (int i = 0; i < 30; i++) { sum += analogRead(BATTERY_PIN); delayMicroseconds(100); }
  digitalWrite(ADC_EN_PIN, LOW);
  return (sum / 30.0f / 4095.0f) * 3.6f * 2.0f * 0.968f;
}

bool connectWiFi(uint32_t timeoutMs = 15000) {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(200);
  return WiFi.status() == WL_CONNECTED;
}

// Downloads exactly FRAME_BYTES into buf. Returns false on any problem.
bool download(uint8_t* buf) {
  WiFiClientSecure client;
  client.setInsecure();  // skips cert check; fine for a public image. See guide to pin a CA instead.
  HTTPClient http;
  http.setTimeout(15000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, IMAGE_URL)) return false;

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("HTTP %d\n", code);
    http.end();
    return false;
  }
  WiFiClient* stream = http.getStreamPtr();
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < FRAME_BYTES && millis() - t0 < 20000) {
    size_t avail = stream->available();
    if (avail) got += stream->readBytes(buf + got, min(avail, FRAME_BYTES - got));
    else if (!http.connected()) break;
    else delay(5);
  }
  http.end();
  Serial.printf("Downloaded %u / %u bytes\n", (unsigned)got, (unsigned)FRAME_BYTES);
  return got == FRAME_BYTES;
}

void drawBatteryWarning(float v) {
  // Small box in the bottom-left corner when the battery is getting low.
  epaper.fillRect(8, H - 34, 150, 26, TFT_WHITE);
  epaper.drawRect(8, H - 34, 150, 26, TFT_BLACK);
  epaper.setTextColor(TFT_BLACK, TFT_WHITE);
  epaper.setCursor(16, H - 28);
  epaper.setTextSize(2);
  epaper.printf("LOW BAT %.2fV", v);
}

bool isNight() {
  configTzTime(TZ_POSIX, "pool.ntp.org", "time.nist.gov");
  struct tm t;
  if (!getLocalTime(&t, 5000)) return false;
  return t.tm_hour >= NIGHT_START || t.tm_hour < NIGHT_END;
}

void sleepFor(uint32_t minutes) {
  Serial.printf("Sleeping %u min\n", (unsigned)minutes);
  Serial.flush();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  // Wake early if D1 is pressed (active low).
  rtc_gpio_pullup_en(BUTTON_PIN);
  rtc_gpio_pulldown_dis(BUTTON_PIN);
  esp_sleep_enable_ext0_wakeup(BUTTON_PIN, 0);
  esp_sleep_enable_timer_wakeup((uint64_t)minutes * 60ULL * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  bootCount++;
  bool buttonWake = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0;
  Serial.printf("Boot #%u%s\n", (unsigned)bootCount, buttonWake ? " (button)" : "");

  float vbat = readBattery();
  Serial.printf("Battery %.2fV\n", vbat);

  if (!connectWiFi()) {
    Serial.println("Wi-Fi failed");
    sleepFor(RETRY_MIN);   // screen keeps showing the last image
  }

  uint8_t* buf = (uint8_t*)(psramFound() ? ps_malloc(FRAME_BYTES) : malloc(FRAME_BYTES));
  if (!buf || !download(buf)) {
    Serial.println("Download failed");
    sleepFor(RETRY_MIN);
  }

  bool lowBat = vbat > 2.5f && vbat < 3.5f;   // >2.5 ignores "no battery, on USB"
  uint32_t crc = crc32_le(0, buf, FRAME_BYTES) ^ (lowBat ? 0xA5A5A5A5 : 0);

  if (crc != lastCrc || buttonWake) {
    epaper.begin();
    epaper.fillScreen(TFT_WHITE);
    epaper.drawBitmap(0, 0, buf, W, H, TFT_BLACK);  // MSB-first, 1 bits drawn black
    if (lowBat) drawBatteryWarning(vbat);
    epaper.update();
    epaper.sleep();
    lastCrc = crc;
    Serial.println("Screen updated");
  } else {
    Serial.println("No change, skipping refresh");
  }
  free(buf);

  sleepFor(isNight() ? NIGHT_REFRESH_MIN : REFRESH_MIN);
}

void loop() {}  // never reached
