# KontAKTMail

Al korrespondance med ansøgeren går
gennem den ene postkasse, over Graph, med certifikat. **Ingen læser den
postkasse** - det gør denne robot.

## Hvad den gør

To ting, og de kommer fra hver sit sted:

| mode | kommer fra | gør |
|---|---|---|
| `send` | kø-element fra KontAKT | Sender én besked, en sagsbehandler har skrevet. Rapporterer det Message-ID, Exchange gav den, tilbage. |
| `poll` | triggerens procesargument | Læser indbakken og fordeler alt i den. |

Sendevejen findes, fordi certifikatet ligger på robotmaskinerne og ikke på
IIS-serveren - web-laget skal være tyndt. KontAKT skriver beskeden i tråden som
`queued` med det samme, så sagsbehandleren ser den; robotten sender den minutter
senere og melder tilbage.

**Message-ID'et er hele pointen med tilbagemeldingen.** Ansøgerens svar bærer
netop det id i `In-Reply-To`, og det er det eneste, der kobler svaret til sagen.
Derfor sender robotten ikke med `/sendMail` (som svarer 202 med tom body og
aldrig røber id'et), men opretter en kladde, læser `internetMessageId` og sender
kladden.

## Indbakken som alarm

Alt, robotten har taget stilling til, flyttes ud af indbakken:

* `KontAKT - på sag` - lagt på en sag i KontAKT
* `KontAKT - afvist` - kunne ikke placeres; afsenderen har fået et autosvar
* `KontAKT - maskinpost` - autosvar, afvisningsbeskeder, nyhedsbreve

**Er indbakken tom, er der intet, der kræver et menneske.** Bliver der noget
liggende, er det en mail, robotten hverken kunne placere eller svare på - og så
skal nogen kigge.

## Hvorfor det er forsvarligt at autosvare fremmede

To lofter, uafhængige af hinanden:

1. `graph_mail.should_ignore` nægter at behandle noget, en maskine har skrevet -
   autosvar, NDR'er, mailinglister, vores egen post der kommer retur. Et
   autosvar kan derfor kun nå et menneske. **Det er mailloop-spærren:** svarer
   man en autosvarer, kan to robotter sende den samme mail frem og tilbage i det
   uendelige.
2. KontAKT tillader ét autosvar pr. adresse pr. døgn (`mail_autoreplies`). Det
   ligger i databasen og ikke i robotten, fordi robotten kan genstartes, køre på
   en anden maskine eller køre to gange - og ingen af delene er en grund til at
   skrive til den samme person igen.

Autosvaret oprettes ikke her. Robotten spørger KontAKT, hvad den skal gøre med
hver mail, og får teksten med i svaret. **Beslutningen om at skrive til en borger
tages aldrig i robotten.**

## Der oprettes aldrig sager fra postkassen

En mail, der ikke kan kobles til en eksisterende sag, bliver **ikke** til en sag.
Afsenderen henvises til selvbetjeningsløsningen eller `post@mtm.aarhus.dk`.
Det er derfor spam og nyhedsbreve aldrig brænder et sagsnummer i GO.

## Matchning

Stærkeste bevis først. Robotten finder kandidaterne, KontAKT slår dem op - kun
KontAKT har `case_emails`:

1. **`In-Reply-To` / `References`** - id'et på den mail, svaret svarer på. Kan
   ikke tastes forkert og overlever citering.
2. **`[KontAKT #35]` i emnet** - synligt, og derfor både redigerbart og
   sletbart, men det eneste, der overlever en ansøger, som skriver en ny mail i
   stedet for at svare. Formatet skal matche `app/cases/sanitize.add_case_tag`.
3. **`conversationId`** - Exchanges egen tråd. Bemærk: Exchange sætter et
   conversationId på **hver** mail, også den første i en helt ny tråd, så det er
   kun et bevis, hvis KontAKT har set id'et før.

En custom header (`X-KontAKT-Case-Id`) er bevidst **ikke** en strategi: næsten
ingen mailklienter sender ukendte headere retur på et svar, så den ville virke i
test med Outlook og fejle i praksis.

## Opsætning i OpenOrchestrator

**Credentials**

| Navn | username | password |
|---|---|---|
| `KontAKTAPI` | KontAKTs base-URL | `X-API-Key` |
| `KontAKTGraph` | tenant id | client id |
| `KontAKTCert` | thumbprint | sti til `.pem`-filen |

`.pem`-filen skal ligge på **samme sti på hver robotmaskine**.

**Kun ÉN trigger.** Et planlagt job, der kører hvert minut med procesargumentet

```json
{"mode": "poll"}
```

Der skal **ikke** oprettes en kø-trigger ved siden af. Når det planlagte job
fyrer, læser robotten først postkassen, og løber derefter kø-løkken, som tømmer
alt hvad KontAKT har lagt i køen - og afslutter så. Ét minutligt job dækker
altså begge veje, og der er ingen proces, der kører konstant.

Uden procesargumentet læses postkassen ikke - så sender robotten kun det, der
ligger i køen. Da ingen holder øje med postkassen, er en trigger, der stille er
blevet slået fra, den fejl, der ville tage længst tid at opdage.

**Kø:** `KontAKTMail` skal oprettes, så KontAKT har et sted at lægge elementerne
- den skal bare ikke have sin egen trigger. Bemærk `QUEUE_ATTEMPTS = 1`, modsat
de andre KontAKT-robotter: en genkørsel efter en afsendelse, der faktisk
lykkedes, lægger en kopi mere i ansøgerens indbakke, og den kan ikke kaldes
tilbage. Fejlen skrives i stedet på beskeden (`case_emails.send_error`) og vises
i tråden.

**To ting at holde øje med ved et minutligt job**

`main.py` kører `pip install --upgrade uv` ved hver start. Ved den her kadence
bliver det 1440 netværkskald i døgnet. Det er hustemplatets opførsel og gør
ingen skade, men det er værd at vide, hvis nogen undrer sig over trafikken.

En kørsel med tunge vedhæftninger kan tage længere end et minut. `MAX_TASK_COUNT`
(50) og `POLL_BATCH` (50) holder én kørsel afgrænset, så en ophobning drænes over
flere kørsler i stedet for i én lang. Sæt dem lavere, hvis kørslerne begynder at
overlappe.

## Afhængigheder

`msal` og `requests`. Ikke `oomtm`, ikke LibreOffice, ikke `cryptography`.
Journalisering til GO er et selvstændigt kø-element på `KontAKTJournalize`, som
ejer GO-forbindelsen - denne robot flytter kun post.

`robot_framework/graph_mail.py` er en **kopi** af `KontAKT/tools/graph_mail.py`,
hvor matchlogikken udvikles og afprøves mod den rigtige postkasse med
`tools/graph_sandbox.py`. Hold de to i takt.
