#pragma once

#include <TFT_eSPI.h>

/**
 * Minimal UC8179 partial refresh extension for Seeed_GFX
 * 
 * The UC8179 controller supports partial (fast) refresh mode which:
 * - Updates faster (~1-2 seconds vs 4-6 seconds)
 * - Less flashing/ghosting during update
 * - Good for frequent content updates
 * 
 * Trade-off: Can accumulate ghosting over many updates, so periodically
 * use full refresh to clear it (e.g., after deep sleep).
 */

/**
 * Perform a partial (fast) refresh update
 * Call this instead of epaper.update() for faster, less flashy updates
 * 
 * @param epd Pointer to EPaper display object
 */
inline void updatePartial(EPaper* epd) {
  // Get the framebuffer from the display (already in 1-bit packed format)
  uint8_t* fb = (uint8_t*)epd->frameBuffer(0);
  if (!fb) return;

  // Calculate buffer size (1 bit per pixel for monochrome)
  int width = epd->width();
  int height = epd->height();
  int bufSize = (width * height) / 8;

  // Wake display if needed
  epd->startWrite();

  // Send partial refresh initialization sequence
  epd->writecommand(0x00);  // Panel Setting
  epd->writedata(0x1F);     // Use internal temperature sensor

  epd->writecommand(0x50);  // VCOM and Data Interval Setting
  epd->writedata(0x10);     // Border output: follow LUT
  epd->writedata(0x07);     // Data polarity

  epd->writecommand(0xE0);  // Cascade Setting (partial mode)
  epd->writedata(0x02);     // Enable partial mode

  epd->writecommand(0xE5);  // Force Temperature
  epd->writedata(0x5A);     // Internal temperature value

  // Push framebuffer to display RAM (0x13 = new image data)
  epd->writecommand(0x13);

  // The framebuffer is already in 1-bit packed format, just send it directly
  for (int i = 0; i < bufSize; i++) {
    epd->writedata(~fb[i]);  // Invert for UC8179
  }

  // Trigger partial refresh
  epd->writecommand(0x12);  // Display Refresh
  delay(10);

  // Wait for busy signal to clear
  while (digitalRead(TFT_BUSY) == 0) {
    delay(10);
  }

  epd->endWrite();
}
