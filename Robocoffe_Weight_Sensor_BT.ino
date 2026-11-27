#include <SoftwareSerial.h>
#include "HX711.h"

// HX711 Load Cell Pins (Rows S of columns 2 and 3)
#define DT  3
#define SCK 2

// Pins for the HC-05 Bluetooth Module
#define BT_RX 10 // Connected to HC-05 TXD
#define BT_TX 11 // Connected to HC-05 RXD (using the voltage divider)

// FACTOR OBTAINED FROM YOUR CALIBRATION
float calibration_factor = -1007.83; 

// Object instances
HX711 scale;
SoftwareSerial BT(BT_RX, BT_TX);

void setup() {
  // Initialize communications
  Serial.begin(9600);
  BT.begin(9600);

  Serial.println("\n=============================================");
  Serial.println("     ROBOCOFFE SYSTEM - WEIGHING MODULE      ");
  Serial.println("=============================================");

  scale.begin(DT, SCK);
  scale.set_scale(calibration_factor);

  // Automatic tare (zeroing) of the empty cradle
  Serial.println("Setting zero (Tare)... Do not touch the robot.");
  scale.tare(15); 

  Serial.println("---------------------------------------------");
  Serial.println("SYSTEM READY! Transmitting data...");
  Serial.println("---------------------------------------------");
  
  BT.println("ROBOCOFFE_READY");
}

void loop() {
  if (scale.is_ready()) {
    // Read the average of 3 fast samples
    float weight = scale.get_units(3);

    // Filter small fluctuations or noise near zero
    if (weight < 0.0 && weight > -1.0) {
      weight = 0.0;
    }

    // 1. Display on the computer's Serial Monitor (USB)
    Serial.print("Weight: ");
    Serial.print(weight, 1);
    Serial.println(" g");

    // 2. Transmit via Bluetooth to the phone / app
    BT.print("WEIGHT:");
    BT.println(weight, 1);

    // Detect if the cup is removed
    if (weight < -50.0) {
      BT.println("EVENT:CUP_REMOVED");
    }

  } else {
    Serial.println("Alert: Loose connection on DT/SCK pins.");
  }

  delay(300); // Constant transmission rate (every 300 ms)
}