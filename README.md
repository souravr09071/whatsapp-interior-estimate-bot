
# WhatsApp AI Interior Estimate Bot

A WhatsApp-based AI proof of concept that estimates home interior fit-out costs from a floor plan or manually entered room details.

The bot is designed for an interior design startup workflow where a customer can upload a floor plan, receive a room-wise interior scope, choose a design tier, select a budget range, and get a preliminary cost estimate.

## Features

- WhatsApp chatbot interface using Twilio WhatsApp Sandbox
- Flask backend webhook for receiving WhatsApp messages
- Cloudflare Tunnel for local testing
- OpenAI-powered floor plan analysis
- Room-wise interior area estimation
- Suggested furniture and modular interior scope
- Modular, loose, and either-category furniture classification
- Indian interior design assumptions such as modular kitchens, wardrobes, lofts, vanities, pooja units, and utility areas
- Style-based pricing tiers
- Budget comparison logic
- Manual fallback flow when the user does not have a floor plan
- Confidence scores, missing information, assumptions, and ambiguity handling
- Fail-safe fallback analysis when the uploaded floor plan cannot be analyzed cleanly

## Tech Stack

- Python
- Flask
- Twilio WhatsApp Sandbox
- OpenAI API
- Cloudflare Tunnel
- python-dotenv
- requests

## Project Structure

```text
whatsapp-interior-bot/
├── app.py
├── pricing.py
├── requirements.txt
├── .env
├── .env.example
├── .gitignore
└── README.md
```

## Bot Flow

```text
User sends "start"
        ↓
Bot asks whether user has a floor plan
        ↓
Option 1: Upload floor plan
        ↓
AI analyzes floor plan room by room
        ↓
Bot returns room-wise summary, furniture scope, missing info, and confidence
        ↓
User confirms or edits analysis
        ↓
User selects design style
        ↓
User selects budget range
        ↓
Bot generates preliminary interior cost estimate
```

If the user does not have a floor plan, the bot asks for home type and room sizes manually, then generates the same kind of room-wise scope and estimate.

## Pricing Tiers

The bot currently uses four style tiers:

| Tier | Description | Rate |
|---|---|---|
| Functional & Beautiful | Smart, simple, practical, great value | ₹950 / sqft |
| Elegant Everyday Living | Premium, elegant, balanced finishes | ₹1400 / sqft |
| Refined Designer Home | Sophisticated, warm, rich detailing | ₹2100 / sqft |
| One-of-a-Kind Dream Home | Signature, luxurious, completely bespoke | ₹3200 / sqft |

These rates are mock values for the PoC and can be updated in `pricing.py`.

## Budget Ranges

The bot compares the generated estimate against these budget ranges:

- Under ₹10L
- ₹10L–₹15L
- ₹15L–₹20L
- ₹20L–₹30L
- More than ₹30L
- Not sure yet

## Setup Instructions

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/whatsapp-interior-bot.git
cd whatsapp-interior-bot
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install flask twilio requests python-dotenv openai
```

Or, if using `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 4. Create a `.env` file

Create a file named `.env` in the project root:

```env
OPENAI_API_KEY=your_openai_api_key_here
TWILIO_ACCOUNT_SID=your_twilio_account_sid_here
TWILIO_AUTH_TOKEN=your_twilio_auth_token_here
```

Do not commit this file to GitHub.

## Running the Bot Locally

### Terminal 1: Start Flask

```bash
cd /Users/rajagopalkrishnareddy/Documents/whatsapp-interior-bot
source venv/bin/activate
kill -9 $(lsof -ti :5001) 2>/dev/null || true
python3 -u app.py
```

Expected output:

```text
🚀 WhatsApp Interior Estimate Bot running on Flask...
Local URL: http://localhost:5001
Webhook path: /whatsapp
Running on http://127.0.0.1:5001
```

Leave this terminal open.

### Terminal 2: Start Cloudflare Tunnel

```bash
cloudflared tunnel --protocol http2 --url http://localhost:5001
```

Cloudflare will generate a public URL like:

```text
https://example-random-words.trycloudflare.com
```

Your Twilio webhook URL will be:

```text
https://example-random-words.trycloudflare.com/whatsapp
```

Leave this terminal open.

## Twilio WhatsApp Sandbox Setup

1. Open Twilio Console.
2. Go to WhatsApp Sandbox settings.
3. Set **When a message comes in** to your Cloudflare webhook URL:

```text
https://your-cloudflare-url.trycloudflare.com/whatsapp
```

4. Set the method to:

```text
POST
```

5. Click **Save**.

## Testing the Bot

On WhatsApp, send:

```text
restart
```

Then choose:

```text
1
```

to upload a floor plan, or:

```text
2
```

to enter room details manually.

## Example Output

```text
Apartment / Home Summary:
- Type: 2BHK apartment
- Relevant interior area considered: 850 sqft
- Area source: estimated
- Confidence: 72%

Room-wise area considered:
- Living Room: 240 sqft
- Kitchen: 110 sqft
- Master Bedroom: 180 sqft
- Bedroom 2: 140 sqft

Kitchen:
- Layout: L-shaped
- Confidence: 70%

Suggested furniture / interior scope:
- Living Room: TV Unit = 1
- Kitchen: Modular Kitchen = 1
- Master Bedroom: Wardrobe with Loft = 1
- Bedroom 2: Wardrobe with Loft = 1

Please confirm:
1. Yes, this looks good
2. I want to edit / correct details
```

## Current Limitations

- This is a local PoC, not a production deployment.
- Cloudflare quick tunnel URLs change every time the tunnel restarts.
- The bot only works while the laptop is awake and both terminals are running.
- Floor plan analysis depends on image clarity.
- Cost estimates are rough and meant for early-stage qualification, not final quotations.
- Room dimensions, wall lengths, and furniture recommendations may require human review.
- Twilio Sandbox is for testing only and requires users to join the sandbox before messaging.

## Future Improvements

- Deploy backend to a cloud server
- Use official WhatsApp Business API setup
- Store user sessions in a database instead of memory
- Add lead capture and CRM integration
- Generate downloadable PDF estimates
- Add vendor matching workflow
- Add admin dashboard
- Improve floor plan parsing accuracy
- Add image generation for different design tiers
- Support multiple languages
- Add payment or booking flow for consultation

## Disclaimer

This project is a proof of concept. Estimates are preliminary and should not be treated as final interior design quotations. A professional designer or sales team should review the floor plan, scope, dimensions, materials, and budget before giving a final quote.