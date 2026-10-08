# Forum Pulse

What Taiwanese retail stock forums are talking about each day, how bullish or
bearish they are about it, and whether that attention or opinion was followed
by the instrument outperforming or underperforming the market.

## Language

### Sources

**Forum**:
A discussion board that Comments are collected from — PTT Stock now, Dcard
Stock later. Every Comment belongs to exactly one.
_Avoid_: site, source, board

**Post**:
A thread on a Forum, with a title, a body and the Comments under it.
_Avoid_: article, thread, 文章

**Post Type**:
The bracketed tag a Post's title carries — 標的, 新聞, 請益, 閒聊 and so on.
_Avoid_: category, flair

**Comment**:
One thing a user wrote at one moment: a Post's body or a single push (推/噓/→)
under it. Dated by its own timestamp, never by the Post it sits under.
_Avoid_: push, reply, message

### Recognising instruments

**Instrument**:
Anything the Forum talks about as a thing to trade — a listed or OTC stock, an
ETF, the index, or an index future.
_Avoid_: ticker, symbol, stock (a stock is only one kind of Instrument)

**Overseas Instrument**:
A stock listed outside Taiwan that the Forum talks about (輝達, 美光, 三星電子,
海力士). Its Excess Return is against its own market's index, counted on its
own trading days.

**Market**:
The one Instrument that stands for the whole Taiwan market — 大盤, 加權, 台指期
and 小台 are all Aliases of it. Judged by its own return, since its Excess
Return is zero by construction. Inverse and leveraged ETFs are not the Market;
they are ordinary Instruments.
_Avoid_: index, 大盤 (as a term), TAIEX

**Alias**:
A string that names an Instrument in a Comment — its code, its official name,
or forum slang like GG or 發哥.
_Avoid_: keyword, nickname

**Ambiguous Alias**:
An Alias that could name more than one Instrument (長榮, 三星), or an Instrument
or nothing at all (統一, 創意, 川寶). The user decides once for good, by context
words, or one Comment at a time; where the user has not, an Alias Call does.

**Alias Call**:
The model's verdict on what an Ambiguous Alias means in one Comment, made from
that Comment and its neighbours. Any rule of the user's overrides it.
_Avoid_: guess, auto-resolve

**Not an Instrument**:
The verdict — the user's, or an Alias Call's — that an Alias, in some or all
Comments, is ordinary text and names nothing.
_Avoid_: false positive, ignored

**Review Queue**:
The Comments holding an Ambiguous Alias nobody has decided yet — no rule and no
Alias Call so far. They count toward no Mention until one is made.

### Measuring the forum

**Mention**:
One user talking about one Instrument on one Forum Day, however many Comments
they wrote about it. The unit everything is counted in.
_Avoid_: hit, occurrence, reference

**Forum Day**:
The Taipei calendar date of a Comment.
_Avoid_: post date, session

**Stance**:
Whether a Mention is bullish, bearish, neutral or mixed on its Instrument —
one verdict over all that user's Comments on it that day. Not the same as how
cheerful the Comments sound: a gleeful short seller is bearish.
_Avoid_: sentiment, mood, polarity

**Author Stance**:
The Stance a 標的 Post's author declares in its title (多 or 空). It is that
author's Stance for that Instrument that day, whatever the model would have
said; the model's agreement with it is how the model is checked.
_Avoid_: label, tag

**Net Stance**:
For one Instrument on one Forum Day, bullish minus bearish Mentions over
bullish plus bearish Mentions; from −1 to +1.
_Avoid_: sentiment score

**Top Mentioned**:
The N Instruments with the most Mentions on a Forum Day.
_Avoid_: hot, trending, popular

**Buzz Spike**:
An Instrument whose Mentions on a Forum Day are several times its own recent
average — sudden attention, as opposed to standing fame.
_Avoid_: trending, surge

### Testing what followed

**Signal**:
An Instrument on a Forum Day on which it was Top Mentioned or a Buzz Spike —
the thing whose aftermath is measured.
_Avoid_: event, trade, pick

**Stance Group**:
Which way the forum leaned on a Signal: Bullish (Net Stance ≥ +0.3), Bearish
(≤ −0.3) or Split (between). A Signal with too few bullish-or-bearish Mentions
has no Stance Group.
_Avoid_: bucket, sentiment class

**Entry**:
The open of the first trading day after a Forum Day — the earliest anyone
could have acted on what the forum said.
_Avoid_: signal price, buy price

**Horizon**:
How long after Entry a return is measured, in trading days: 1D = 1, 1W = 5,
1M = 21, 3M = 63.
_Avoid_: holding period, window

**Excess Return**:
An Instrument's dividend-inclusive return over a Horizon minus the Market's
total return over the same Horizon (for an Overseas Instrument, its own
market's index). The figure every result is judged by.
_Avoid_: alpha, outperformance
