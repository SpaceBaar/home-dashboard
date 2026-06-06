#include <DHT.h>

// Configuration Pins
#define DHTPIN 2          // DHT11 Data Pin connected to Digital 2
#define DHTTYPE DHT11     // Specifying the sensor type
#define FAN_PWM_PIN 9     // Fan PWM control MUST be Pin 9 on Uno for 25kHz timer hack

// Temperature Thresholds (Celsius)
const float TEMP_MIN = 32.0; // Fan turns on at minimum speed (30%) above this temp
const float TEMP_MAX = 42.0; // Fan ramps up to 100% maximum speed at this temp

DHT dht(DHTPIN, DHTTYPE);

void setup() {
  Serial.begin(9600);
  dht.begin();

  pinMode(FAN_PWM_PIN, OUTPUT);
  
  // TCCR1A and TCCR1B configuration registers for Timer 1
  TCCR1A = _BV(WGM11);                                // Phase Correct PWM (COM bits managed dynamically)
  TCCR1B = _BV(WGM13) | _BV(CS10);                    // Mode 10 (ICR1 defines TOP), Prescaler = 1
  ICR1 = 320;                                         // F_CPU / (2 * 25000) = 16000000 / 50000 = 320
  
  setFanSpeed(0); // Start with fan completely off
  Serial.println("Smart Solar Box Cooling System Initialized.");
}

void loop() {
  // Wait 2 seconds between measurements (DHT11 is slow)
  delay(2000);

  float currentTemp = dht.readTemperature();

  // Check if reading failed
  if (isnan(currentTemp)) {
    Serial.println("Error: Failed to read from DHT sensor! Running fan at 100% for safety.");
    setFanSpeed(100); 
    return;
  }

  int fanDutyCycle = 0;

  // Thermal Control Logic
  if (currentTemp < TEMP_MIN) {
    fanDutyCycle = 0; // Box is cool, completely stop fan
  } 
  else if (currentTemp >= TEMP_MAX) {
    fanDutyCycle = 100; // Temperature limit exceeded, maximum cooling
  } 
  else {
    // Linearly map the temperature range (32C to 42C) to a safe fan speed range (30% to 100%)
    fanDutyCycle = map(currentTemp, TEMP_MIN, TEMP_MAX, 30, 100);
  }

  setFanSpeed(fanDutyCycle);

  // Debugging Telemetry sent to Serial Monitor
  Serial.print("Internal Box Temp: ");
  Serial.print(currentTemp);
  Serial.print("°C | Target Fan Speed: ");
  Serial.print(fanDutyCycle);
  Serial.println("%");
}

// Function to safely handle hardware PWM and true 0% logic
void setFanSpeed(int dutyPercentage) {
  if (dutyPercentage <= 0) {
    // Disconnect Pin 9 from Timer 1 and force a pure 0V digital LOW
    TCCR1A &= ~_BV(COM1A1); 
    digitalWrite(FAN_PWM_PIN, LOW);
  } 
  else {
    // Reconnect Pin 9 to Timer 1 for 25kHz hardware PWM
    TCCR1A |= _BV(COM1A1); 
    
    if (dutyPercentage > 100) dutyPercentage = 100;
    
    int registerValue = map(dutyPercentage, 0, 100, 0, 320);
    OCR1A = registerValue; 
  }
}