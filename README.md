# HLS Proxy pro VLC na Smart TV

Jednoduchý vícevláknový HTTP/HLS proxy server určený pro sledování chráněných online streamů na televizi přes přehrávač VLC (Android TV, Apple TV, LG webOS, Fire TV atd.).

## Proč je proxy potřeba?

Běžné přehrávače v televizích nedokážou přehrát streamy, které:
1. **Vyžadují specifické HTTP hlavičky** (`Referer`, `Origin`, `User-Agent`) nejen pro playlist `.m3u8`, ale i pro každý jednotlivý video segment `.ts`. Bez nich server vrací chybu `403 Forbidden`.
2. **Expirují tokeny každých pár minut:** Některé streamy vyžadují pravidelnou obnovu přístupového tokenu přes interní API.
3. **Maskují video segmenty jako PNG obrázky:** Někteří poskytovatelé vkládají na začátek každého `.ts` segmentu maskovací PNG hlavičku a posílají `Content-Type: image/png`. Běžný přehrávač soubor vyhodnotí jako obrázek a selže. Tato proxy maskovací hlavičku za běhu automaticky odřízne a do TV pošle čistý `video/mp2t` (MPEG-TS).

---

## Hlavní výhody

- **Jediná neměnná adresa pro televizi:** Ve VLC na TV máte stále uloženou stejnou adresu (`http://<IP_VASEHO_PC>:8080/live.m3u8`), kterou nikdy nemusíte složitě přepisovat dálkovým ovladačem.
- **Plynulé přepínání streamů bez pádu přehrávače (Seamless Switching):** Když změníte stream (přepnete na jiný zápas), VLC na televizi **nespadne ani se nezastaví**. Proxy plynule naváže sekvenční čísla (`MEDIA-SEQUENCE`), vloží HLS značku `#EXT-X-DISCONTINUITY` a televizní přehrávač plynule přejde na nový obraz.
- **Automatická detekce změn:** Stream můžete změnit přes webové rozhraní (`http://<IP>:8080/`), nebo prostým přepsáním souboru `stream_url.txt` – proxy změnu souboru okamžitě detekuje.
- **Automatická volba nejvyšší kvality:** Pokud zadáte Master Playlist s více rozlišeními, proxy automaticky vybere nejvyšší kvalitu (1080p) a televizi servíruje čistý media stream.
- **Automatická obnova tokenů:** U vybraných platforem proxy automaticky obnovuje token před vypršením platnosti bez přerušení vysílání.
- **Vícevláknový server (`ThreadingHTTPServer`):** Zabraňuje výpadkům a timeoutům při současném stahování video segmentů a aktualizaci playlistu.

---

## Požadavky

- **Python 3.8+**
- Knihovny:
  ```bash
  pip install requests urllib3
  ```

---

## Rychlý start

1. **Vložení odkazu na stream:**
   - Odkaz můžete vložit do souboru `stream_url.txt` (jeden řádek s URL).
   - Nebo ho zadáte později přes webové rozhraní.

2. **Spuštění proxy:**
   - Ve Windows stačí spustit dvojklikem:
     ```cmd
     start_stream.bat
     ```
   - Nebo přes příkazovou řádku:
     ```bash
     python proxy.py
     ```
   - Lze také předat URL přímo jako argument:
     ```bash
     python proxy.py "https://adresa-streamu.m3u8"
     ```

3. **Přehrávání na TV:**
   - V aplikaci **VLC** na televizi zvolte **Otevřít síťový proud** (Open Network Stream).
   - Zadejte adresu zobrazenou v konzoli po spuštění:
     ```text
     http://<IP_VASEHO_PC>:8080/live.m3u8
     ```
