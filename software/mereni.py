#!/usr/bin/env python3
"""
Zaznam ohybove zkousky z Arduina (tenzometr + HX711), rucni otaceni.

Pouziti na Windows (Prikazovy radek ve slozce s timto souborem):
    py -m pip install pyserial matplotlib
    py mereni.py VZOREK_ID SIRKA_mm TLOUSTKA_mm HMOTNOST_g
napr.
    py mereni.py PLA_30_01 15.02 4.05 7.4
Desetinna tecka i carka funguji (7.4 i 7,4).
Kdyby program Arduino nenasel, pridej port na konec:  ... 7.4 COM4
Bez grafu:  ... 7.4 --bez-grafu

Postup:
  1. Trn zvedni NAD tycku, nesmi se ji dotykat. Enter = tara.
  2. Priprava: dotoc trn k tycce, sila se zobrazuje zive. Nic se neuklada.
     Enter = start zaznamu a pipani.
  3. Toc plynule ctvrt otacky za 5 s (pipnuti).
  4. Po lomu pockej asi 2 s (program zmeri nulu) a stiskni Enter.
     Kdyz tycka neprazkne: odtoc trn nahoru, pockej 2 s a stiskni Enter.
  5. Vyber typ poruseni (1 uplny lom, 2 castecny lom, 3 bez lomu) a pripadne poznamku.
Vysledky: data/VZOREK_ID.csv (nahore udaje o vzorku, pod nimi prubeh),
          data/VZOREK_ID.png (graf),
          data/vysledky.csv (jeden radek na vzorek).

Fmax je vzdy namerena hodnota. Sila na konci zaznamu se jen zapise pro
kontrolu (sloupec nula_po_lomu_N); u castecneho lomu nula byt nemusi.
Pruhyb se nemeri, proto se modul pruznosti nepocita.
"""

import csv
import os
import queue
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit("Chybi knihovna pyserial. Nainstaluj ji prikazem: py -m pip install pyserial")

try:
    import matplotlib.pyplot as plt
    HAVE_PLT = True
except ImportError:
    HAVE_PLT = False

SPAN_MM = 100.0          # rozpeti podper
BEEP_S = 5.0             # ctvrt otacky za 5 s
WARN_N = 176.0           # 18 kg, blizi se rozsah tenzometru 20 kg
OVERLOAD_N = 245.0       # 25 kg
ZERO_WINDOW_S = 1.0      # z poslednich 1 s po lomu se urci nula
ZERO_WARN_N = 1.0        # vetsi posun nuly = varovani

SUMMARY_HEADER = ["vzorek", "datum", "b_mm", "h_mm", "hmotnost_g", "Fmax_N", "pevnost_MPa",
                  "Fmax_namereno_N", "nula_po_lomu_N", "cas_do_Fmax_s", "lom", "poznamka"]

# rozumne rozsahy vstupu: odhali prehozene poradi cisel nebo preklep
RANGES = {"sirka": (10.0, 20.0, "mm"), "tloustka": (2.0, 6.0, "mm"), "hmotnost": (1.0, 25.0, "g")}

# slozka data vedle skriptu, at se spusti odkudkoli
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def find_port():
    """Najde Arduino na Windows (COMx), Macu i Linuxu."""
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    # nejdriv prevodnik CH340 (VID 0x1A86), pak cokoli s 'CH340', 'USB-SERIAL' nebo 'Arduino'
    for p in ports:
        if p.vid == 0x1A86:
            return p.device
    for p in ports:
        text = f"{p.description} {p.manufacturer}".upper()
        if "CH34" in text or "USB-SERIAL" in text or "ARDUINO" in text or "USBSERIAL" in p.device.upper():
            return p.device
    if len(ports) == 1:
        return ports[0].device
    if ports:
        print("Nalezene porty:")
        for p in ports:
            print(f"  {p.device}  {p.description}")
        sys.exit("Nepoznal jsem, ktery je Arduino. Spust znovu s portem na konci, napr.: py mereni.py ID 15.02 4.05 7.4 COM3")
    sys.exit("Arduino nenalezeno. Je pripojene datovym kabelem a zavreny Seriovy monitor v Arduino IDE?")


def read_force(line):
    """Z radku 'cas_ms,sila_g,sila_N' vrati (cas_ms, sila_N), jinak None."""
    if not line or line.startswith("#") or line.startswith("cas"):
        return None
    parts = line.split(",")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), float(parts[2])
    except ValueError:
        return None


def flexural(F, b, h):
    return 3 * F * SPAN_MM / (2 * b * h * h)          # MPa (N/mm2)



def median3(vals, i):
    w = vals[max(0, i - 1): i + 2]
    return sorted(w)[len(w) // 2]


class LivePlot:
    """Zivy graf sila/cas, obnovuje se cca 3x za sekundu."""

    def __init__(self, sample, stop_flag):
        plt.ion()
        self.fig, self.ax = plt.subplots(figsize=(9, 6))
        self.fig.canvas.mpl_connect(
            "key_press_event", lambda e: stop_flag.set() if e.key == "enter" else None)
        try:
            self.fig.canvas.manager.set_window_title(f"Ohybova zkouska {sample}")
        except Exception:
            pass
        (self.line,) = self.ax.plot([], [], lw=1.6, color="#0F6E56")
        self.ax.set_title(f"Vzorek {sample}")
        self.ax.set_xlabel("cas [s]")
        self.ax.set_ylabel("sila [N]")
        self.ax.grid(True, alpha=0.3)
        self.info = self.ax.text(0.02, 0.97, "", transform=self.ax.transAxes, va="top",
                                 fontsize=15, family="monospace",
                                 bbox=dict(facecolor="white", alpha=0.85, edgecolor="#cccccc"))
        plt.show(block=False)
        plt.pause(0.1)

    def alive(self):
        return plt.fignum_exists(self.fig.number)

    def update(self, ts, fs, F, f_max, turns):
        self.line.set_data(ts, fs)
        self.ax.set_xlim(0, max(10.0, ts[-1] * 1.1))
        self.ax.set_ylim(min(0.0, min(fs)) - 2, max(10.0, f_max * 1.15))
        self.info.set_text(f"F    = {F:7.2f} N  ({F / 9.80665:6.3f} kg)\n"
                           f"Fmax = {f_max:7.2f} N\n"
                           f"ctvrt otacek: {turns}")
        self.fig.canvas.draw_idle()
        plt.pause(0.001)


def save_final_plot(ts, fs, i_max, F_max, sigma, sample, path, show):
    plt.ioff()
    plt.close("all")
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.plot(ts, fs, lw=1.6, color="#0F6E56")
    ax.plot(ts[i_max], F_max, "o", color="#D85A30")
    ax.annotate(f"Fmax = {F_max:.1f} N\nσf = {sigma:.1f} MPa", (ts[i_max], F_max),
                textcoords="offset points", xytext=(-120, -50), fontsize=12)
    ax.set_title(f"Vzorek {sample}")
    ax.set_xlabel("cas [s]")
    ax.set_ylabel("sila [N]")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    if show:
        print("Graf zustava otevreny, zavrenim okna program skonci.")
        plt.show()
    plt.close(fig)


def summary_path():
    """data/vysledky.csv; starou tabulku s jinymi sloupci prejmenuje."""
    path = os.path.join(DATA_DIR, "vysledky.csv")
    if os.path.exists(path):
        with open(path, newline="") as fh:
            first = next(csv.reader(fh), [])
        if first != SUMMARY_HEADER:
            old = os.path.join(DATA_DIR, "vysledky_pilot.csv")
            if os.path.exists(old):
                old = os.path.join(DATA_DIR, f"vysledky_stare_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            os.rename(path, old)
            print(f"(Stara tabulka vysledku prejmenovana na {old})")
    return path


def write_summary(row, ask):
    """Pripise radek do vysledky.csv; kdyz je soubor otevreny v Excelu, pocka."""
    while True:
        try:
            summary = summary_path()
            new = not os.path.exists(summary)
            with open(summary, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(SUMMARY_HEADER)
                w.writerow(row)
            return summary
        except PermissionError:
            ask("\nSoubor vysledky.csv je asi otevreny v Excelu. Zavri ho a stiskni Enter... ")


def check_ranges(values, ask):
    bad = [f"{k} = {v} {RANGES[k][2]} (cekam {RANGES[k][0]:g} az {RANGES[k][1]:g})"
           for k, v in values.items() if not RANGES[k][0] <= v <= RANGES[k][1]]
    if bad:
        print("POZOR, neobvykla hodnota: " + "; ".join(bad))
        print("Poradi je: SIRKA TLOUSTKA HMOTNOST.")
        if ask("Pokracovat presto? (a/n) ").strip().lower() not in ("a", "ano", "y"):
            sys.exit("Ukonceno, spust znovu se spravnymi cisly.")


def main():
    use_plot = HAVE_PLT and "--bez-grafu" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    usage = ("Pouziti: py mereni.py VZOREK_ID SIRKA_mm TLOUSTKA_mm HMOTNOST_g [PORT] [--bez-grafu]\n"
             "napr.:   py mereni.py PLA_30_01 15.02 4.05 7.4")
    if len(args) not in (4, 5):
        sys.exit(usage)
    sample = args[0]
    try:
        b, h, m = (float(a.replace(",", ".")) for a in args[1:4])
    except ValueError:
        sys.exit("Sirka, tloustka a hmotnost musi byt cisla.\n" + usage)
    if any(c in sample for c in '\\/:*?"<>|'):
        sys.exit('ID vzorku nesmi obsahovat znaky \\ / : * ? " < > |')
    check_ranges({"sirka": b, "tloustka": h, "hmotnost": m}, input)
    if not HAVE_PLT and "--bez-grafu" not in sys.argv:
        print("(Zivy graf neni k dispozici, chybi matplotlib: py -m pip install matplotlib)")

    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, f"{sample}.csv")
    png_path = os.path.join(DATA_DIR, f"{sample}.png")
    if os.path.exists(out_path):
        sys.exit(f"Soubor {out_path} uz existuje, zvol jine ID vzorku.")

    port = args[4] if len(args) == 5 else find_port()
    try:
        ser = serial.Serial(port, 115200, timeout=1)
    except serial.SerialException as e:
        if "PermissionError" in str(e) or "odep" in str(e) or "denied" in str(e).lower():
            sys.exit(f"Port {port} je obsazeny jinym programem. Zavri Seriovy monitor "
                     "(nejlepe cele Arduino IDE) a jina okna s mereni.py, pak to spust znovu.")
        sys.exit(f"Nelze otevrit port {port}: {e}")
    time.sleep(2.5)                      # Arduino se po pripojeni restartuje
    ser.reset_input_buffer()

    print(f"Pripojeno na {port}. Vzorek {sample}, b = {b} mm, h = {h} mm, m = {m} g.")
    print("Vloz tycku a ZVEDNI TRN NAD NI, trn se tycky nesmi dotykat.")
    input("Pak stiskni Enter (provede se tara)... ")
    ser.reset_input_buffer()
    ser.write(b"t\n")
    deadline = time.time() + 6
    tared = False
    while time.time() < deadline:
        if "tara hotova" in ser.readline().decode(errors="ignore"):
            tared = True
            break
    if not tared:
        print("POZOR: Arduino nepotvrdilo taru. Zkontroluj, ze je nahrany spravny program.")
    ser.readline()                       # zahodit pripadny neuplny radek

    # cteni klavesnice v jednom vlakne: Enter ukonci zaznam, dalsi radky jsou odpovedi
    lines = queue.Queue()

    def reader():
        while True:
            try:
                lines.put(input())
            except (EOFError, KeyboardInterrupt):
                return

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def ask(prompt):
        print(prompt, end="", flush=True)
        while True:
            try:
                return lines.get(timeout=0.2)
            except queue.Empty:
                if not reader_thread.is_alive():
                    try:
                        return input()
                    except (EOFError, KeyboardInterrupt):
                        return ""

    # priprava: zivy udaj sily, nic se neuklada, Enter = start
    print("\nTARA HOTOVA. Ted je cas na pripravu: dotoc trn k tycce, srovnej vse.")
    print("Sila se zobrazuje zive (trn by mel jen lehce dosednout, asi do 0,3 N).")
    print("Az budes pripraveny, stiskni ENTER = start zaznamu.\n")
    shown = 0.0
    rec = None
    while lines.empty():
        rec = read_force(ser.readline().decode(errors="ignore").strip())
        if rec and time.time() - shown > 0.3:
            F = rec[1]
            print(f"\r  priprava:  F = {F:7.2f} N  ({F / 9.80665 * 1000:6.0f} g)     ", end="", flush=True)
            shown = time.time()
    lines.get_nowait()
    print()
    if rec and rec[1] > 2.0:
        print(f"  (pozor: pri startu uz trn tlaci {rec[1]:.1f} N, nevadi, jen dalsi vzorky startuj podobne)")
    ser.reset_input_buffer()
    ser.readline()                       # zahodit pripadny neuplny radek

    stop_flag = threading.Event()
    plot = LivePlot(sample, stop_flag) if use_plot else None
    print("ZAZNAM BEZI. Toc plynule ctvrt otacky za kazde pipnuti.")
    print("Po lomu pockej asi 2 s a stiskni ENTER. (Funguje i Ctrl+C nebo zavreni grafu.)")
    print("Kdyz tycka neprazkne: odtoc trn nahoru, pockej 2 s a stiskni ENTER.\n")
    ts, fs = [], []
    t_start = time.time()
    next_beep = t_start + BEEP_S
    turns = 0
    f_max = 0.0
    last_print = 0.0
    last_plot = 0.0
    win = []
    ms0 = None
    warned = False

    try:
        while True:
            if not lines.empty():
                lines.get_nowait()
                stop_flag.set()
            if stop_flag.is_set():
                print("\nZaznam ukoncen (Enter).")
                break
            line = ser.readline().decode(errors="ignore").strip()
            now = time.time()
            if now >= next_beep:
                turns += 1
                print(f"\a  >>> ctvrt otacky c. {turns}")
                next_beep += BEEP_S
            rec = read_force(line)
            if rec is None:
                continue
            ms, F = rec
            if ms0 is None:
                ms0 = ms
            t = (ms - ms0) / 1000.0          # cas z hodin Arduina, nezavisi na zpozdeni PC
            ts.append(t)
            fs.append(F)
            win = (win + [F])[-3:]
            f_max = max(f_max, sorted(win)[len(win) // 2])
            if now - last_print > 0.5:
                print(f"  t = {t:6.1f} s   F = {F:7.2f} N   ({F / 9.80665 * 1000:7.0f} g)")
                last_print = now
            if plot and now - last_plot > 0.3:
                if not plot.alive():
                    print("\nOkno s grafem zavreno, zaznam ukoncen.")
                    break
                plot.update(ts, fs, F, f_max, turns)
                last_plot = now
            if F > OVERLOAD_N:
                print("\a!!! PRETIZENI, prestan tocit !!!")
            elif F > WARN_N and not warned:
                print("\a  pozor: sila nad 18 kg, rozsah tenzometru je 20 kg")
                warned = True
    except KeyboardInterrupt:
        print("\nZaznam ukoncen rucne.")
    finally:
        ser.close()

    if not fs:
        sys.exit("Nebyla zaznamenana zadna data.")

    # Fmax z medianu 3 sousednich hodnot (jeden chybny vzorek ho neurci)
    filt = [median3(fs, i) for i in range(len(fs))]
    i_max = max(range(len(fs)), key=lambda i: filt[i])
    F_raw = filt[i_max]

    # Fmax je vzdy namerena hodnota, nula se uz neopravuje (tara se dela s trnem
    # nad tyckou). Sila po lomu se jen zapise pro kontrolu: u uplneho lomu ma byt
    # kolem nuly, u castecneho lomu tycka dal nese zatez a nula byt nemusi.
    tail = [f for t, f in zip(ts, fs) if t >= ts[-1] - ZERO_WINDOW_S and t > ts[i_max]]
    offset = sum(tail) / len(tail) if len(tail) >= 5 else None
    F_max = F_raw
    sigma = flexural(F_max, b, h)

    print(f"\nVysledek {sample}:")
    print(f"  Fmax       = {F_max:.2f} N ({F_max / 9.80665:.3f} kg)")
    print(f"  pevnost    = {sigma:.1f} MPa")
    if offset is not None:
        print(f"  sila na konci zaznamu = {offset:.2f} N")

    print("\nJak se tycka porusila?")
    print("  1 = uplny lom (rozlomila se na dva kusy)")
    print("  2 = castecny lom (nalomila se, ale drzi vcelku)")
    print("  3 = bez lomu (jen se ohnula)")
    broke = None
    while broke is None:
        ans = ask("Volba 1, 2 nebo 3 (Enter = 1): ").strip()
        broke = {"": "uplny", "1": "uplny", "2": "castecny", "3": "zadny"}.get(ans)
        if broke is None:
            print("  Napis jen 1, 2 nebo 3.")
    if broke == "uplny" and offset is not None and abs(offset) > ZERO_WARN_N:
        print(f"  POZOR: po uplnem lomu ukazuje vaha {offset:.2f} N misto nuly.")
        print("         Zkontroluj, jestli trn pri tare nelezel na tycce nebo jestli se nezasekl ulomek.")
        print("         Vysledek se neopravuje, jen to pripadne napis do poznamky.")
    note = ask("Poznamka, napr. lom mimo stred (Enter = zadna): ").strip()

    row = [sample, time.strftime("%Y-%m-%d %H:%M"), b, h, m, f"{F_max:.2f}",
           f"{sigma:.2f}", f"{F_raw:.2f}", f"{offset:.2f}" if offset is not None else "",
           f"{ts[i_max]:.1f}", broke, note]

    # prubeh zkousky: nahore udaje o vzorku a vysledky, pod prazdnym radkem data
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        for key, val in zip(SUMMARY_HEADER, row):
            w.writerow([key, val])
        w.writerow([])
        w.writerow(["cas_s", "sila_N"])
        for t, f in zip(ts, fs):
            w.writerow([f"{t:.3f}", f"{f:.3f}"])

    summary = write_summary(row, ask)
    print(f"\nUlozeno: {out_path} a radek v {summary}")

    if HAVE_PLT:
        save_final_plot(ts, fs, i_max, F_max, sigma, sample, png_path, show=use_plot)
        print(f"Graf ulozen: {png_path}")


if __name__ == "__main__":
    main()
