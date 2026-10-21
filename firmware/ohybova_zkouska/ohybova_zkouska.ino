// Ohybova zkouska: tenzometr 20 kg + HX711 + Arduino Nano
// Zapojeni: HX711 DT -> D3, SCK -> D2, VCC -> 5V, GND -> GND
// Knihovna: "HX711 Arduino Library" (Bogdan Necula), instalace pres Spravce knihoven
// Seriovy monitor: 115200 baud, konec radku "Newline"
//
// Prikazy do serioveho monitoru:
//   t        tara (vynulovani, nic nesmi lezet na pripravku navic)
//   c        kalibrace zavazim 278,05 g: po tare poloz zavazi a posli prikaz c
//   c 500    kalibrace jinym zavazim (hmotnost v gramech, tecka i carka)
//   f        vypis kalibracniho faktoru
// Kalibrace se ulozi do pameti Arduina a plati i po odpojeni.

#include <HX711.h>
#include <EEPROM.h>

const int PIN_DT  = 3;
const int PIN_SCK = 2;

const float WARN_G     = 18000.0;   // varovani nad 18 kg
const float OVERLOAD_G = 25000.0;   // nad 25 kg prestat tocit
const float CAL_MASS_G = 278.05;    // kalibracni zavazi (prikaz c bez cisla)

HX711 scale;
float calFactor = 1.0;               // surove jednotky na gram
bool calibrated = false;             // dokud neni kalibrace, nehlasime pretizeni

struct Cal { uint32_t magic; float factor; };
const uint32_t MAGIC = 0x48583731;   // kontrola platnosti ulozene kalibrace
const int EE_ADDR = 0;

void loadCal() {
  Cal c;
  EEPROM.get(EE_ADDR, c);
  if (c.magic == MAGIC && !isnan(c.factor) && c.factor != 0.0) { calFactor = c.factor; calibrated = true; }
}

void saveCal() {
  Cal c = { MAGIC, calFactor };
  EEPROM.put(EE_ADDR, c);
}

void setup() {
  Serial.begin(115200);
  scale.begin(PIN_DT, PIN_SCK);
  loadCal();
  scale.set_scale(calFactor);
  delay(500);
  scale.tare(20);
  Serial.println(F("# Pripraveno. Prikazy: t = tara, c = kalibrace zavazim 278,05 g, f = faktor"));
  Serial.print(F("# kalibracni faktor: "));
  Serial.println(calFactor, 4);
  if (!calibrated) Serial.println(F("# ZATIM NEKALIBROVANO: cisla jsou surove hodnoty, ne gramy. Udelej t, poloz zavazi 278,05 g a posli c."));
  Serial.println(F("cas_ms,sila_g,sila_N"));
}

void handleCommand() {
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;
  char cmd = line.charAt(0);

  if (cmd == 't') {
    scale.tare(20);
    Serial.println(F("# tara hotova"));
  } else if (cmd == 'c') {
    String num = line.substring(1);
    num.trim();
    num.replace(',', '.');                     // prijme i desetinnou carku
    float mass = (num.length() == 0) ? CAL_MASS_G : num.toFloat();
    if (mass <= 0) {
      Serial.println(F("# zadej hmotnost v gramech, napr. c 278.05"));
      return;
    }
    Serial.print(F("# kalibruji zavazim "));
    Serial.print(mass, 2);
    Serial.println(F(" g, nehybej s nim..."));
    float raw = scale.get_value(30);           // prumer 30 mereni bez tary
    if (raw == 0) {
      Serial.println(F("# zadny signal, zkontroluj zapojeni"));
      return;
    }
    calFactor = raw / mass;                    // zaporne znamenko se srovna samo
    scale.set_scale(calFactor);
    saveCal();
    calibrated = true;
    Serial.print(F("# nova kalibrace ulozena, faktor: "));
    Serial.println(calFactor, 4);
  } else if (cmd == 'f') {
    Serial.print(F("# kalibracni faktor: "));
    Serial.println(calFactor, 4);
  }
}

void loop() {
  if (Serial.available()) handleCommand();

  if (scale.is_ready()) {
    float g = scale.get_units(1);
    float n = g * 0.00980665;
    Serial.print(millis());
    Serial.print(',');
    Serial.print(g, 1);
    Serial.print(',');
    Serial.println(n, 3);

    if (calibrated) {
      if (g > OVERLOAD_G)  Serial.println(F("# !!! PRETIZENI nad 25 kg, prestan tocit !!!"));
      else if (g > WARN_G) Serial.println(F("# pozor, sila nad 18 kg"));
    }
  }
}
