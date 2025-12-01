/*
 * 7.5" ePaper Panel - Smart Dashboard Display
 * 
 * USE AT YOUR OWN RISK. THIS CODE IS PROVIDED AS-IS WITHOUT WARRANTY.
 * YOU MAY HAVE TO ADJUST THE CODE TO SUIT YOUR SPECIFIC HARDWARE.
 * PLEASE DOUBLE CHECK YOUR HARDWARE AND UNDERSTAND THIS CODE BEFORE UPLOADING.
 * 
 * FEATURES:
 * - Downloads and displays 1 bit color PNG images from HTTP server
 * - Light sleep between refreshes (preserves display state, fast wake)
 * - Deep sleep during night hours (12am-5am for maximum battery savings)
 * - NTP time synchronization for accurate scheduling
 * - Built-in ghosting prevention (handled by library)
 * - Automatic retry on download failures
 * - Battery monitoring (sends percentage to server as ?battery=<0-100>)
 * 
 * TESTED HARDWARE:
 * - reTerminal E1001 (Recommended) (https://www.seeedstudio.com/reTerminal-E1001-p-6534.html)
 * - XIAO 7.5 ePaper display (https://www.seeedstudio.com/XIAO-7-5-ePaper-Panel-p-6416.html)
 * - XIAO ESP32-C3 / ESP32-S3 microcontrollers
 * - 7.5" UC8179 ePaper display (800x480, monochrome, supports partial refresh)
 * - 2000mAh LiPo battery
 * 
 * REQUIRED LIBRARIES:
 * - PNGdec by Larry Bank (Arduino Library Manager)
 * - Seeed_GFX (GitHub: https://github.com/Seeed-Studio/Seeed_GFX)
 * 
 * CONFIGURATION:
 * - Double check driver.h for display settings
 * - Set WiFi credentials below
 * - Set server IP and port
 * - Adjust timezone offset for your location
 * 
 * AUTHOR: Kyle Turman
 * LICENSE: MIT
 * VERSION: 1.0.1
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <PNGdec.h>
#include <GxEPD2_BW.h>
#include <GxEPD2_7C.h>
#include <time.h>
// #include "partial-refresh.h"

// ==================== CONFIGURATION ====================

// Define ePaper SPI pins
#define EPD_SCK_PIN 7
#define EPD_MOSI_PIN 9
#define EPD_CS_PIN 10
#define EPD_DC_PIN 11
#define EPD_RES_PIN 12
#define EPD_BUSY_PIN 13

// Select the ePaper driver to use
// 0: reTerminal E1001 (7.5'' B&W)
// 1: reTerminal E1002 (7.3'' Color)
#define EPD_SELECT 1

#if (EPD_SELECT == 0)
#define GxEPD2_DISPLAY_CLASS GxEPD2_BW
#define GxEPD2_DRIVER_CLASS GxEPD2_750_GDEY075T7  // 7.5'' B&W driver
#elif (EPD_SELECT == 1)
#define GxEPD2_DISPLAY_CLASS GxEPD2_7C
#define GxEPD2_DRIVER_CLASS GxEPD2_730c_GDEP073E01  // 7.3'' Color driver
#endif

#define MAX_DISPLAY_BUFFER_SIZE 16000

#define MAX_HEIGHT(EPD) \
  (EPD::HEIGHT <= MAX_DISPLAY_BUFFER_SIZE / (EPD::WIDTH / 8) \
     ? EPD::HEIGHT \
     : MAX_DISPLAY_BUFFER_SIZE / (EPD::WIDTH / 8))

// Initialize display object
GxEPD2_DISPLAY_CLASS<GxEPD2_DRIVER_CLASS, MAX_HEIGHT(GxEPD2_DRIVER_CLASS)>
  display(GxEPD2_DRIVER_CLASS(/*CS=*/EPD_CS_PIN, /*DC=*/EPD_DC_PIN,
                              /*RST=*/EPD_RES_PIN, /*BUSY=*/EPD_BUSY_PIN));

SPIClass hspi(HSPI);

// WiFi configuration
const char* WIFI_SSID = "Airtel_SpaceBaar";
const char* WIFI_PASSWORD = "21072410";

// Server configuration
const char* SERVER_IP = "raspberrypi.local";
const int SERVER_PORT = 7272;

// Main dashboard image path
const char* IMAGE_PATH = "/dashboard/image";

// Time configuration (used for deep sleep scheduling)
const long GMT_OFFSET_SEC = 19800;    // PST (UTC-8). Adjust for your timezone
const int DAYLIGHT_OFFSET_SEC = 0;    // Add 1 hour if DST active, 0 otherwise
const int DEEP_SLEEP_START_HOUR = 0;  // Enter deep sleep at midnight
const int DEEP_SLEEP_END_HOUR = 5;    // Wake from deep sleep at 5am
const char* NTP_SERVER = "pool.ntp.org";

// Display configuration
const unsigned long REFRESH_INTERVAL = 600000;  // 10 minutes in milliseconds
const int CONNECT_TIMEOUT = 20000;              // WiFi connection timeout (ms)
const int HTTP_TIMEOUT = 45000;                 // HTTP request timeout (ms)

// ==================== HARDWARE CONFIGURATION ====================
// Display settings - adjust these for different display sizes
#define DISPLAY_WIDTH 800
#define DISPLAY_HEIGHT 480

// CPU Frequency (MHz) - Lower = better battery life
// ESP32-S3: 240, 160, 80, 40, 20, 10 MHz
// ESP32-C3: 160, 80, 40, 20, 10 MHz (no 240MHz)
// 80MHz recommended for good balance of speed and power efficiency
#define CPU_FREQ_MHZ 80

// Battery monitoring pins (ESP32-S3 specific)
#define BATTERY_ADC_PIN 1      // GPIO1 - Battery voltage ADC
#define BATTERY_ENABLE_PIN 21  // GPIO21 - Battery monitoring enable

// Button pins (reTerminal E1002 specific)
#define REFRESH_BUTTON_PIN 0   // GPIO0 - Refresh button (active low)
#define SLIDE_BUTTON_PIN 47    // GPIO47 - Slide change button (active low)

// ==================== DEBUG CONFIGURATION ====================
// Set to 1 to enable serial debugging, 0 for production (saves power)
#define DEBUG_ENABLED 1

#if DEBUG_ENABLED
#define DEBUG_PRINT(x) Serial.print(x)
#define DEBUG_PRINTLN(x) Serial.println(x)
#define DEBUG_PRINTF(...) Serial.printf(__VA_ARGS__)
#else
#define DEBUG_PRINT(x)
#define DEBUG_PRINTLN(x)
#define DEBUG_PRINTF(...)
#endif


// ==================== RTC MEMORY ====================
// Variables stored in RTC memory persist through light sleep but reset on deep sleep

RTC_DATA_ATTR int bootCount = 0;                // Total number of boots/wakes
RTC_DATA_ATTR bool timeInitialized = false;     // Whether NTP time has been synced
RTC_DATA_ATTR unsigned long lastWakeTime = 0;   // Unix timestamp of last wake
RTC_DATA_ATTR bool displayInitialized = false;  // Track if display hardware is initialized (persists through light sleep)

// Button state tracking
bool refreshButtonPressed = false;
bool slideButtonPressed = false;

// ==================== GLOBAL VARIABLES ====================

PNG png;  // PNG decoder instance

// PNG buffer (allocated dynamically during download)
uint8_t* pngBuffer = nullptr;
size_t pngBufferSize = 0;
int renderLine = 0;

// ==================== FUNCTION DECLARATIONS ====================

// Initialization & Setup
void initializeHardware();
void prepareFirstBoot();
void ensureTimeSync();

// Core Loop Functions
bool shouldEnterDeepSleep();
void enterDeepSleep(unsigned long seconds);
void enterLightSleep();
bool refreshDashboard();

// Network & Data
bool connectWiFi();
bool downloadPNG();
bool syncTimeWithNTP();

// Display & UI
void updateDisplay(bool usePartialRefresh = false);
void showBootStatus(const char* message, bool success, bool isError = false);

// PNG Decoding
void decodePNG();
int pngDrawCallback(PNGDRAW* pDraw);

// Utilities
bool isDeepSleepTime(int hour);
unsigned long getSecondsUntil5AM(int currentHour, int currentMinute);
void shutdownWiFi();
int getBatteryPercentage();
bool checkButtonPress();
void handleButtonPress();

// ==================== SETUP ====================

void setup() {
#if DEBUG_ENABLED
  Serial.begin(115200);
  delay(1000);
#endif

  setCpuFrequencyMhz(CPU_FREQ_MHZ);
  bootCount++;

  DEBUG_PRINTLN("\n========================================");
  DEBUG_PRINTLN("ePaper Smart Display Client");
  DEBUG_PRINTLN("========================================");
  DEBUG_PRINTF("Boot #%d | Free Memory: %d bytes\n", bootCount, ESP.getFreeHeap());
  DEBUG_PRINTF("CPU Frequency: %d MHz\n", getCpuFrequencyMhz());

  // Check wake reason
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  bool wokeFromButton = (wakeup_reason == ESP_SLEEP_WAKEUP_EXT0) || (wakeup_reason == ESP_SLEEP_WAKEUP_EXT1_BITMASK);

  if (wokeFromButton) {
    DEBUG_PRINTLN("Woke from deep sleep due to button press");
  }

  initializeHardware();
  prepareFirstBoot();
  ensureTimeSync();

  DEBUG_PRINTLN("Initialization complete\n");
}

// ==================== MAIN LOOP ====================

void loop() {
  DEBUG_PRINTLN("--- Refresh Cycle Start ---");

  // Small delay after wake to allow buttons to stabilize
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  if (wakeup_reason == ESP_SLEEP_WAKEUP_EXT0 || wakeup_reason == ESP_SLEEP_WAKEUP_EXT1_BITMASK) {
    delay(500); // Allow button to stabilize
  }

  // Check for button presses first
  if (checkButtonPress()) {
    handleButtonPress();
  }

  // Check if we should enter deep sleep (night time)
  if (shouldEnterDeepSleep()) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
      unsigned long sleepSeconds = getSecondsUntil5AM(timeinfo.tm_hour, timeinfo.tm_min);
      enterDeepSleep(sleepSeconds);
      // Never returns - device resets on wake
    }
  }

  // Refresh dashboard
  bool success = refreshDashboard();

  if (!success && bootCount == 1) {
    delay(5000);  // Show error message on first boot
  }

  // Enter light sleep until next refresh
  enterLightSleep();

  DEBUG_PRINTLN("--- Refresh Cycle End ---\n");
}

// ==================== INITIALIZATION & SETUP ====================

/**
 * Initialize display hardware based on wake type
 * Handles both cold boot/deep sleep wake and light sleep wake
 */
void initializeHardware() {
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  bool wokeFromDeepSleep = (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER && !displayInitialized);

  pinMode(EPD_RES_PIN, OUTPUT);
  pinMode(EPD_DC_PIN, OUTPUT);
  pinMode(EPD_CS_PIN, OUTPUT);

  // Initialize button pins with internal pull-up
  pinMode(REFRESH_BUTTON_PIN, INPUT_PULLUP);
  pinMode(SLIDE_BUTTON_PIN, INPUT_PULLUP);

  // Initialize SPI
  hspi.begin(EPD_SCK_PIN, -1, EPD_MOSI_PIN, -1);
  display.epd2.selectSPI(hspi, SPISettings(4000000, MSBFIRST, SPI_MODE0));

  // Initialize display
  display.init(115200);
}

/**
 * Prepare display for first boot with status messages
 * Only runs on bootCount == 1
 */
void prepareFirstBoot() {
  // Display initialization is handled in showBootStatus for first boot
}

/**
 * Ensure system time is synchronized with NTP
 * Keeps WiFi connected for subsequent dashboard download
 */
void ensureTimeSync() {
  if (!timeInitialized) {
    showBootStatus("Connecting WiFi...", true);
    DEBUG_PRINTLN("Connecting to WiFi for NTP sync...");

    if (connectWiFi()) {
      showBootStatus("Syncing time...", true);
      syncTimeWithNTP();
      timeInitialized = true;
      // Keep WiFi connected for dashboard download
    } else {
      showBootStatus("Connecting WiFi...", false, true);
      DEBUG_PRINTLN("WARNING: NTP sync failed - time may be inaccurate");
    }
  }
}

// ==================== CORE LOOP FUNCTIONS ====================

/**
 * Check if current time is during deep sleep hours
 * @return true if should enter deep sleep
 */
bool shouldEnterDeepSleep() {
  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    DEBUG_PRINTF("Current time: %02d:%02d:%02d\n",
                 timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    return isDeepSleepTime(timeinfo.tm_hour);
  }
  DEBUG_PRINTLN("WARNING: Failed to get local time");
  return false;
}

/**
 * Enter deep sleep mode for specified duration
 * Shuts down WiFi and display, clears RTC flags
 * @param seconds Duration to sleep in seconds
 */
void enterDeepSleep(unsigned long seconds) {
  DEBUG_PRINTF("Night mode: Entering deep sleep for %lu seconds (until 5am)\n", seconds);
  DEBUG_PRINTLN("Display will reinitialize on wake\n");

#if DEBUG_ENABLED
  Serial.flush();
#endif

  shutdownWiFi();
  display.hibernate();

  // Mark that we need to reinitialize after deep sleep
  displayInitialized = false;
  timeInitialized = false;

  // Configure wake sources
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);

  // Enable timer wakeup
  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);

  // Enable external wakeup on button press (active low)
  esp_sleep_enable_ext0_wakeup((gpio_num_t)REFRESH_BUTTON_PIN, 0); // Wake on LOW
  esp_sleep_enable_ext1_wakeup_bitmask(((uint64_t)1) << SLIDE_BUTTON_PIN, ESP_EXT1_WAKEUP_ANY_LOW);

  // Enter deep sleep (ultra-low power ~10-20µA)
  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);
  esp_deep_sleep_start();
  // Device resets on wake - execution never continues past this point
}

/**
 * Enter light sleep mode for refresh interval
 * Preserves display state and RAM (~0.8mA)
 */
void enterLightSleep() {
  shutdownWiFi();
  display.hibernate();

  DEBUG_PRINTF("Entering light sleep for %lu minutes\n", REFRESH_INTERVAL / 60000);

#if DEBUG_ENABLED
  Serial.flush();
#endif

  // Configure wake sources
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);

  // Enable timer wakeup
  esp_err_t timer_err = esp_sleep_enable_timer_wakeup(REFRESH_INTERVAL * 1000ULL);

  if (timer_err != ESP_OK) {
    DEBUG_PRINTF("CRITICAL ERROR: Failed to configure timer wakeup (err: %d)\n", timer_err);
    DEBUG_PRINTLN("Restarting device...\n");
#if DEBUG_ENABLED
    Serial.flush();
#endif
    delay(5000);
    ESP.restart();
  }

  // Enable external wakeup on button press (active low)
  esp_sleep_enable_ext0_wakeup((gpio_num_t)REFRESH_BUTTON_PIN, 0); // Wake on LOW
  esp_sleep_enable_ext1_wakeup_bitmask(((uint64_t)1) << SLIDE_BUTTON_PIN, ESP_EXT1_WAKEUP_ANY_LOW);

  // Enter light sleep
  esp_err_t sleep_result = esp_light_sleep_start();

  // === Wake up - execution continues here ===
  if (sleep_result == ESP_OK) {
    DEBUG_PRINTLN("\n========================================");
    DEBUG_PRINTLN("Woke from light sleep");

    esp_sleep_wakeup_cause_t wakeup_cause = esp_sleep_get_wakeup_cause();
    if (wakeup_cause == ESP_SLEEP_WAKEUP_EXT0 || wakeup_cause == ESP_SLEEP_WAKEUP_EXT1_BITMASK) {
      DEBUG_PRINTLN("Woke due to button press");
    } else if (wakeup_cause != ESP_SLEEP_WAKEUP_TIMER) {
      DEBUG_PRINTF("WARNING: Unexpected wake source: %d\n", wakeup_cause);
    }

    display.refresh();
  } else {
    DEBUG_PRINTF("CRITICAL ERROR: Light sleep failed (err: %d)\n", sleep_result);
    DEBUG_PRINTLN("Restarting device...\n");
#if DEBUG_ENABLED
    Serial.flush();
#endif
    delay(5000);
    ESP.restart();
  }
}

/**
 * Download and display dashboard image
 * Handles WiFi connection, download, and display update
 * @return true if successful, false on error
 */
bool refreshDashboard() {
  bool usePartial = displayInitialized;  // Use partial refresh for light sleep wakes
  showBootStatus("Loading dashboard...", true);

  // Try downloading with retry logic
  for (int attempt = 1; attempt <= 2; attempt++) {
    if (attempt > 1) {
      DEBUG_PRINTF("Retry attempt %d/2\n", attempt);
      delay(5000);
    }

    // Connect WiFi if not already connected
    bool wifiConnected = (WiFi.status() == WL_CONNECTED) || connectWiFi();

    if (wifiConnected && downloadPNG()) {
      updateDisplay(usePartial);
      return true;
    }
  }

  // All attempts failed
  DEBUG_PRINTLN("ERROR: All download attempts failed");
  showBootStatus("Loading dashboard...", false, true);
  return false;
}

// ==================== DISPLAY FUNCTIONS ====================

/**
* Update the ePaper display with the downloaded PNG image
* @param usePartialRefresh If true, uses fast partial refresh (for frequent updates)
*                          If false, uses full refresh (for deep sleep wakes, better quality)
*/
void updateDisplay(bool usePartialRefresh) {
  if (pngBuffer == nullptr || pngBufferSize == 0) {
    DEBUG_PRINTLN("ERROR: No image data to display");
    return;
  }

  if (usePartialRefresh) {
    DEBUG_PRINTLN("Updating display (partial refresh)...");
    display.setPartialWindow(0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT);
  } else {
    DEBUG_PRINTLN("Updating display (full refresh)...");
    display.setFullWindow();
  }

  display.firstPage();
  do {
    if (bootCount != 1) {
      display.fillScreen(GxEPD_WHITE);
    }
    decodePNG();
  } while (display.nextPage());

  // Free PNG buffer - we're done with it
  free(pngBuffer);
  pngBuffer = nullptr;
  pngBufferSize = 0;

  DEBUG_PRINTLN("Display updated successfully");
}

/**
* Decode PNG data from memory into the ePaper display buffer
*/
void decodePNG() {
  int result = png.openRAM(pngBuffer, pngBufferSize, pngDrawCallback);

  if (result != PNG_SUCCESS) {
    DEBUG_PRINTF("ERROR: Failed to open PNG (code: %d)\n", result);
    return;
  }

  DEBUG_PRINTF("Decoding PNG: %dx%d pixels\n", png.getWidth(), png.getHeight());

  if (png.getWidth() != DISPLAY_WIDTH || png.getHeight() != DISPLAY_HEIGHT) {
    DEBUG_PRINTF("WARNING: Size mismatch! Expected %dx%d\n", DISPLAY_WIDTH, DISPLAY_HEIGHT);
  }

  renderLine = 0;
  result = png.decode(nullptr, 0);

  if (result != PNG_SUCCESS) {
    DEBUG_PRINTF("ERROR: PNG decode failed (code: %d)\n", result);
  }

  png.close();
}

/**
 * PNG decoder callback - called for each line of the image
 * Handles 1-bit, 2-bit, 4-bit, and 8-bit indexed PNGs
 * For 1-bit PNGs, pixels are packed 8 per byte
 * 
 * IMPORTANT: Server should provide indexed PNG format
 * - 800x480 pixels
 * - 1-bit, 2-bit, 4-bit or 8-bit indexed color
 * - 2-color palette for 1-bit (black and white)
 */
int pngDrawCallback(PNGDRAW* pDraw) {
  int y = pDraw->y;
  int width = pDraw->iWidth;

  // Verify this is an indexed PNG
  if (pDraw->iPixelType != PNG_PIXEL_INDEXED) {
    DEBUG_PRINTLN("ERROR: PNG must be indexed format!");
    return 0;  // Stop decoding
  }

  uint8_t* pixels = (uint8_t*)pDraw->pPixels;
  uint8_t* palette = (uint8_t*)pDraw->pPalette;
  int bpp = pDraw->iBpp;  // Bits per pixel (1, 2, 4, or 8)

  // Process each pixel in this line
  for (int x = 0; x < width && x < DISPLAY_WIDTH; x++) {
    uint8_t paletteIndex;

    // Extract palette index based on bit depth
    if (bpp == 8) {
      // 8-bit: one pixel per byte
      paletteIndex = pixels[x];
    } else if (bpp == 4) {
      // 4-bit: two pixels per byte
      int byteIndex = x / 2;
      int pixelInByte = x % 2;
      paletteIndex = (pixels[byteIndex] >> (pixelInByte == 0 ? 4 : 0)) & 0x0F;
    } else if (bpp == 2) {
      // 2-bit: four pixels per byte
      int byteIndex = x / 4;
      int pixelInByte = x % 4;
      paletteIndex = (pixels[byteIndex] >> (6 - pixelInByte * 2)) & 0x03;
    } else {  // bpp == 1
      // 1-bit: eight pixels per byte (MSB first)
      int byteIndex = x / 8;
      int bitInByte = x % 8;
      paletteIndex = (pixels[byteIndex] >> (7 - bitInByte)) & 0x01;
    }

    // Get RGB color from palette
    uint16_t color;
    if (palette != nullptr && paletteIndex < 256) {
      uint8_t r = palette[paletteIndex * 3];
      uint8_t g = palette[paletteIndex * 3 + 1];
      uint8_t b = palette[paletteIndex * 3 + 2];

      // Calculate luminosity - brightness > 127 = white, else black
      uint8_t brightness = (r * 299 + g * 587 + b * 114) / 1000;
      color = (brightness > 127) ? GxEPD_WHITE : GxEPD_BLACK;
    } else {
      // Invalid palette - default to white
      color = GxEPD_WHITE;
    }

    // Draw pixel to ePaper buffer
    if (y < DISPLAY_HEIGHT && x < DISPLAY_WIDTH) {
      display.drawPixel(x, y, color);
    }
  }

  renderLine++;
  return 1;  // Continue decoding
}

// ==================== NETWORK FUNCTIONS ====================

/**
 * Connect to WiFi network with timeout
 * @return true if connected successfully, false on timeout
 */
bool connectWiFi() {
  DEBUG_PRINTF("Connecting to WiFi: %s...", WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > CONNECT_TIMEOUT) {
      DEBUG_PRINTLN(" TIMEOUT");
      return false;
    }
    delay(500);
    DEBUG_PRINT(".");
  }

  DEBUG_PRINTF(" Connected (IP: %s)\n", WiFi.localIP().toString().c_str());
  return true;
}

/**
 * Download PNG image from HTTP server into memory
 * @return true if download successful, false on error
 */
bool downloadPNG() {
  // Get battery percentage (0-100, or -1 if not supported)
  int batteryPercent = getBatteryPercentage();

  // Build URL with battery parameter if supported
  String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + IMAGE_PATH;
  if (batteryPercent >= 0) {
    url += "?battery=" + String(batteryPercent);
    DEBUG_PRINTF("Battery level: %d%%\n", batteryPercent);
  } else {
    DEBUG_PRINTLN("Battery monitoring not supported on this hardware");
  }

  DEBUG_PRINTF("Downloading: %s\n", url.c_str());

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT);
  http.begin(url);

  int httpCode = http.GET();

  if (httpCode != HTTP_CODE_OK) {
    DEBUG_PRINTF("ERROR: HTTP request failed (code: %d)\n", httpCode);
    http.end();
    return false;
  }

  int contentLength = http.getSize();
  DEBUG_PRINTF("Content length: %d bytes\n", contentLength);

  // Validate content length
  // 150KB limit works for both ESP32-S3 (512KB RAM) and ESP32-C3 (400KB RAM)
  // Typical 800x480 1-bit PNG is 5-50KB, so plenty of headroom
  if (contentLength <= 0 || contentLength > 150000) {
    DEBUG_PRINTLN("ERROR: Invalid content length");
    http.end();
    return false;
  }

  // Allocate buffer for PNG data
  pngBuffer = (uint8_t*)malloc(contentLength);
  if (!pngBuffer) {
    DEBUG_PRINTF("ERROR: Failed to allocate %d bytes\n", contentLength);
    http.end();
    return false;
  }

  pngBufferSize = contentLength;

  // Download data
  WiFiClient* stream = http.getStreamPtr();
  size_t bytesRead = 0;

  while (http.connected() && bytesRead < contentLength) {
    size_t available = stream->available();
    if (available) {
      size_t toRead = min(available, contentLength - bytesRead);
      size_t got = stream->readBytes(pngBuffer + bytesRead, toRead);
      bytesRead += got;
    }
    delay(1);
  }

  http.end();

  if (bytesRead != contentLength) {
    DEBUG_PRINTF("ERROR: Download incomplete (%d/%d bytes)\n", bytesRead, contentLength);
    free(pngBuffer);
    pngBuffer = nullptr;
    pngBufferSize = 0;
    return false;
  }

  DEBUG_PRINTLN("Download complete");
  return true;
}

// ==================== TIME MANAGEMENT ====================

/**
 * Synchronize device time with NTP server
 * @return true if sync successful, false on failure
 */
bool syncTimeWithNTP() {
  DEBUG_PRINTLN("Syncing time with NTP server...");

  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);

  // Wait up to 10 seconds for time sync
  struct tm timeinfo;
  int retries = 10;
  while (!getLocalTime(&timeinfo) && retries > 0) {
    delay(1000);
    retries--;
  }

  if (retries > 0) {
#if DEBUG_ENABLED
    Serial.println(&timeinfo, "Time synchronized: %A, %B %d %Y %H:%M:%S");
#endif
    return true;
  } else {
    DEBUG_PRINTLN("ERROR: NTP sync timeout");
    return false;
  }
}

/**
 * Check if current hour is during deep sleep period (12am-5am)
 * @param hour Current hour (0-23)
 * @return true if in deep sleep period
 */
bool isDeepSleepTime(int hour) {
  return (hour >= DEEP_SLEEP_START_HOUR && hour < DEEP_SLEEP_END_HOUR);
}

/**
 * Calculate seconds until 5:00 AM from current time
 * @param currentHour Current hour (0-23)
 * @param currentMinute Current minute (0-59)
 * @return Seconds until 5:00 AM (with 2-minute buffer)
 */
unsigned long getSecondsUntil5AM(int currentHour, int currentMinute) {
  int hoursUntil5AM;

  if (currentHour < DEEP_SLEEP_END_HOUR) {
    // Same day - hours until 5am
    hoursUntil5AM = DEEP_SLEEP_END_HOUR - currentHour;
  } else {
    // Next day - hours until midnight + 5 hours
    hoursUntil5AM = (24 - currentHour) + DEEP_SLEEP_END_HOUR;
  }

  // Convert to seconds, subtract current minutes, add 2-minute buffer
  unsigned long seconds = (hoursUntil5AM * 3600) - (currentMinute * 60) + 120;

  return seconds;
}

// ==================== BOOT STATUS DISPLAY ====================

/**
 * Show boot status messages on first boot only
 * Displays messages line-by-line with checkmarks or X marks
 * Uses partial refresh to avoid flashing
 * 
 * @param message Status message to display
 * @param success True for checkmark (✓), false for X mark (✕)
 * @param isError True to show error details (optional)
 */
static int statusLineY = 20;  // Track current Y position for status messages

void showBootStatus(const char* message, bool success, bool isError) {
  // Only show status on first boot
  if (bootCount != 1) return;

  // Reset line position on first call
  static bool firstCall = true;
  if (firstCall) {
    statusLineY = 20;
    display.setFullWindow();
    display.firstPage();
    do {
      display.fillScreen(GxEPD_WHITE);
      firstCall = false;
    } while (display.nextPage());
  }

  const int MSG_X = 20;
  const int LINE_HEIGHT = 30;

  // Set text properties and draw
  display.setFullWindow();
  display.firstPage();
  do {
    display.setTextColor(GxEPD_BLACK);
    display.setTextSize(2);  // Larger text for readability
    display.setCursor(MSG_X, statusLineY);
    display.print(message);
    display.print(" ");
    if (success) {
      display.print("✓");  // Unicode checkmark
    } else {
      display.print("✕");  // Unicode X mark
    }
  } while (display.nextPage());

  // Move to next line for next status message
  statusLineY += LINE_HEIGHT;

  DEBUG_PRINTF("Boot status: %s %s\n", message, success ? "✓" : "✕");
}

// ==================== UTILITY FUNCTIONS ====================

/**
 * Safely shut down WiFi to prevent interference with sleep timer
 */
void shutdownWiFi() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  delay(500);  // Give WiFi time to fully shut down
}

/**
 * Check for button presses with debouncing
 * @return true if a button was pressed
 */
bool checkButtonPress() {
  static unsigned long lastCheckTime = 0;
  static bool lastRefreshState = HIGH;
  static bool lastSlideState = HIGH;
  const unsigned long DEBOUNCE_DELAY = 200; // 200ms debounce

  unsigned long currentTime = millis();

  // Only check every 50ms to avoid excessive polling
  if (currentTime - lastCheckTime < 50) {
    return false;
  }
  lastCheckTime = currentTime;

  // Read button states (active low)
  bool refreshState = digitalRead(REFRESH_BUTTON_PIN);
  bool slideState = digitalRead(SLIDE_BUTTON_PIN);

  // Check for refresh button press (HIGH to LOW transition)
  if (lastRefreshState == HIGH && refreshState == LOW) {
    refreshButtonPressed = true;
    DEBUG_PRINTLN("Refresh button pressed");
    delay(DEBOUNCE_DELAY); // Simple debounce
    return true;
  }

  // Check for slide button press (HIGH to LOW transition)
  if (lastSlideState == HIGH && slideState == LOW) {
    slideButtonPressed = true;
    DEBUG_PRINTLN("Slide button pressed");
    delay(DEBOUNCE_DELAY); // Simple debounce
    return true;
  }

  // Update last states
  lastRefreshState = refreshState;
  lastSlideState = slideState;

  return false;
}

/**
 * Handle button press events
 */
void handleButtonPress() {
  if (refreshButtonPressed) {
    DEBUG_PRINTLN("Handling refresh button press");
    showBootStatus("Manual refresh...", true);

    // Force a full refresh of the dashboard
    bool success = refreshDashboard();

    if (success) {
      DEBUG_PRINTLN("Manual refresh completed successfully");
    } else {
      DEBUG_PRINTLN("Manual refresh failed");
      showBootStatus("Manual refresh...", false, true);
      delay(3000); // Show error for 3 seconds
    }

    refreshButtonPressed = false;
  }

  if (slideButtonPressed) {
    DEBUG_PRINTLN("Handling slide button press");
    // For now, just log it. Could implement slide changing functionality later
    showBootStatus("Slide change...", true);
    delay(1000);
    slideButtonPressed = false;
  }
}
int getBatteryPercentage() {
  // LiPo battery voltage range
  const float MIN_VOLTAGE = 3.0;  // Empty battery
  const float MAX_VOLTAGE = 4.2;  // Fully charged battery

  // Configure battery monitoring pins
  static bool initialized = false;
  if (!initialized) {
    pinMode(BATTERY_ENABLE_PIN, OUTPUT);
    analogReadResolution(12);                            // 12-bit resolution (0-4095)
    analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);  // Full range 0-3.3V
    initialized = true;
  }

  // Enable battery monitoring circuit
  digitalWrite(BATTERY_ENABLE_PIN, HIGH);
  delay(10);  // Allow circuit to stabilize

  // Read voltage in millivolts
  int mv = analogReadMilliVolts(BATTERY_ADC_PIN);

  // Disable battery monitoring to save power
  digitalWrite(BATTERY_ENABLE_PIN, LOW);

  // Calculate actual battery voltage (2x due to voltage divider)
  float batteryVoltage = (mv / 1000.0) * 2.0;

  // Check if battery is present (voltage should be > 2.5V if battery exists)
  if (batteryVoltage < 2.5) {
    DEBUG_PRINTLN("No battery detected or voltage too low");
    return -1;
  }

  // Calculate percentage
  float percentage = ((batteryVoltage - MIN_VOLTAGE) / (MAX_VOLTAGE - MIN_VOLTAGE)) * 100.0;

  // Clamp to 0-100 range
  if (percentage < 0) percentage = 0;
  if (percentage > 100) percentage = 100;

  DEBUG_PRINTF("Battery voltage: %.2fV (raw: %dmV)\n", batteryVoltage, mv);

  return (int)percentage;
}
