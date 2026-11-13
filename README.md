# 🎬 Kino Reels Bot

Telegram'ga kino tashlaysiz. Bot uning eng qiziq joylarini topib, **10 ta vertikal (1080×1920) reels** qilib qaytaradi. Ularni Instagram yoki TikTok'ga to'g'ridan-to'g'ri joylash mumkin.

## Bot nima qiladi

- 2 GB gacha kino qabul qiladi (video yoki MP4/MKV/AVI fayl).
- Kinoning har bir soniyasiga ball qo'yadi: **ovoz balandligi**, **harakat** va **sahna almashinuvi tezligi** bo'yicha. Keyin eng yuqori ball olgan, bir-birini takrorlamaydigan 10 ta joyni tanlaydi.
- Boshidagi logotiplar va oxiridagi titrlar tashlab ketiladi.
- Kesish sahna boshidan boshlanadi, boshi va oxiri silliq (fade) chiqadi.
- Kinodagi qora chiziqlar avtomatik olib tashlanadi. O'rtada kino, tepasi va pasti qora fon.
- Ovoz telefon uchun balandlashtiriladi. Kinoda o'zbekcha audio yo'lak bo'lsa, o'sha olinadi.
- Kino yuborilgach, bot **kino kodi** va **matn** so'raydi (ikkalasi ixtiyoriy, tugma bilan o'tkazib yuborish mumkin). Yozilsa, ular videoning tepasida chiroyli shriftda chiqadi. Matnda `*yulduzcha*` ichiga olingan so'zlar sariq rangli bo'ladi.
- Har bir reels ostida kinoning qaysi qismi ekanligi yoziladi (masalan, `1:12:40–1:13:20`).

## 1. Kerakli kalitlar

1. **Bot tokeni:** Telegram'da @BotFather → `/newbot` → token nusxa oling.
2. **API_ID va API_HASH:** https://my.telegram.org → telefon raqam bilan kiring → *API development tools* → istalgan nom bilan ilova yarating. Bu kalitlar botga 20 MB dan katta fayllarni (2 GB gacha) yuklab olish imkonini beradi.

## 2. Serverga o'rnatish

Arxivni serverga yuklang va oching:

```bash
scp kino-reels-bot.zip root@SERVER_IP:/opt/
ssh root@SERVER_IP
cd /opt && unzip kino-reels-bot.zip && cd kino-reels-bot
cp .env.example .env
nano .env          # API_ID, API_HASH, BOT_TOKEN ni yozing
```

### 1-usul: Docker (tavsiya etiladi)

```bash
curl -fsSL https://get.docker.com | sh      # Docker o'rnatilmagan bo'lsa
docker compose up -d --build
docker compose logs -f                      # "Bot ishga tushdi" chiqsa — tayyor
```

### 2-usul: Docker'siz (Ubuntu / Debian)

```bash
sudo apt update && sudo apt install -y ffmpeg fonts-dejavu-core python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python bot.py                      # sinab ko'rish, Ctrl+C bilan to'xtatish
```

Bot doim ishlab turishi va server o'chib-yonganda o'zi ishga tushishi uchun (papka `/opt/kino-reels-bot` bo'lishi kerak):

```bash
sudo cp kino-reels-bot.service /etc/systemd/system/
sudo systemctl enable --now kino-reels-bot
journalctl -u kino-reels-bot -f             # loglarni ko'rish
```

## 3. Sozlash

1. Botga `/start` yozing. U sizning **ID** raqamingizni ko'rsatadi.
2. Shu ID ni `.env` faylidagi `ALLOWED_USERS=` ga yozing, aks holda serveringizdan begonalar ham foydalanadi.
3. Botni qayta ishga tushiring: `docker compose restart` yoki `systemctl restart kino-reels-bot`.

| Sozlama | Ma'nosi | Standart |
|---|---|---|
| `CLIP_COUNT` | Nechta reels | 10 |
| `CLIP_LEN` | Har biri necha soniya | 40 |
| `KADR_ZOOM` | `1.3` qilsangiz, kadr kattalashadi (yon tomonlari biroz kesiladi) | 1.0 |
| `ALLOWED_USERS` | Ruxsat berilgan ID lar, vergul bilan | hamma |

Botdagi buyruqlar:
- `/start` — yordam va ID raqamingiz
- `/uzunlik 30` — reels uzunligini o'zgartirish (15–90 soniya)

## Server talablari va tezlik

- Kamida 2 ta CPU yadro, 2 GB RAM. Bo'sh disk kino hajmidan kamida 1,5 baravar katta bo'lishi kerak.
- 2 soatlik kinoni tahlil qilish 1–3 daqiqa davom etadi. 2 yadroli serverda har bir reels taxminan 1 daqiqada tayyor bo'ladi, ya'ni 10 tasi uchun 10 daqiqacha ketadi. Yadro qancha ko'p bo'lsa, shuncha tez ishlaydi.
- Bir vaqtda bitta kino ishlanadi, qolganlari navbatda kutadi. Bot navbatdagi o'rningizni aytib turadi.

## Botsiz sinash

Server terminalida to'g'ridan-to'g'ri ishga tushirish mumkin:

```bash
venv/bin/python cli.py kino.mp4 --soni 10 --uzunlik 40 --matn "Kino nomi" --chiqish natija/
```

## Bilish kerak

- Bot qiziq joyni **ovoz va harakatga qarab** topadi. Jang, quvish, portlash va baland musiqali sahnalarni yaxshi topadi. Jim, lekin kuchli dialogli sahnalarni o'tkazib yuborishi mumkin. Shuning uchun 10 ta variant beriladi va eng yaxshisini o'zingiz tanlaysiz.
- **Mualliflik huquqi:** boshqalarning kinosidan olingan reels Instagram yoki TikTok'da bloklanishi yoki ovozi o'chirilishi mumkin.
