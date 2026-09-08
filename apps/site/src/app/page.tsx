/**
 * The landing page.
 *
 * A server component with no client JavaScript at all: every animation here is
 * CSS. That is a deliberate performance choice, not a limitation - the page
 * loads on a mid-range Android over patchy mobile data, and shipping a
 * megabyte of animation library to make text fade in would cost the conversion
 * it is meant to win.
 */

const FEATURES = [
  {
    icon: "📸",
    title: "Photograph any question",
    body: "Snap the page. It reads printed text and handwriting, works through the question, and sends back the steps - not just the answer.",
  },
  {
    icon: "🧮",
    title: "Maths that is actually checked",
    body: "Algebra and calculus answers are verified by a symbolic engine before you see them. If the working does not hold up, it says so instead of guessing.",
  },
  {
    icon: "📄",
    title: "PDFs and worksheets",
    body: "Send a whole worksheet and ask for question 4. It reads the pages, finds what you asked for, and leaves the rest alone.",
  },
  {
    icon: "🎤",
    title: "Voice notes",
    body: "Too long to type? Record it. Explain the problem out loud the way you would to a friend and get a written answer back.",
  },
  {
    icon: "📊",
    title: "Diagrams and graphs",
    body: "Ask it to plot a function or draw a free-body diagram and it sends a real, correctly-labelled image straight to your chat.",
  },
  {
    icon: "🎯",
    title: "Practice, quizzes and revision",
    body: "Ask for more questions like the one you just got wrong. It generates them, marks your answers, and remembers what you keep tripping on.",
  },
];

const STEPS = [
  {
    title: "Subscribe in a minute",
    body: "Fill in your name, your WhatsApp number, and what you want your tutor to be called. Pay Rs 100.",
  },
  {
    title: "Get a message",
    body: "Your tutor says hello on WhatsApp the moment payment clears. Nothing to install, no app, no login.",
  },
  {
    title: "Just start asking",
    body: "Type it, photograph it, or record it. Homework at 11pm, revision before an exam, a concept that never made sense.",
  },
];

export default function HomePage() {
  return (
    <>
      <section className="hero">
        <div className="shell hero__grid">
          <div>
            <span className="eyebrow reveal d1">
              <span className="pulse" aria-hidden="true" />
              Live on WhatsApp
            </span>

            <h1 className="reveal d2">
              Your study buddy,
              <br />
              already in your chats.
            </h1>

            <p className="hero__lede reveal d3">
              TutorTwin is an AI tutor that lives inside WhatsApp. Send a photo of
              your homework, a worksheet, or a voice note - and get a proper
              worked explanation back in seconds. Every day, at any hour, for
              every subject.
            </p>

            <div className="hero__cta reveal d4">
              <a className="btn btn--lg" href="/payment">
                Get subscription - ₹100
              </a>
              <span className="price-note">
                <strong>₹100</strong> for 30 days · no app to install
              </span>
            </div>
          </div>

          {/* A staged conversation rather than a screenshot: it shows the
              product working, stays crisp on every screen, and weighs nothing. */}
          <div className="phone reveal d3" aria-hidden="true">
            <div className="phone__screen">
              <div className="phone__bar">
                <div className="phone__avatar">AS</div>
                <div>
                  <div className="phone__who">Anita Ma&apos;am · TutorTwin</div>
                  <div className="phone__status">online</div>
                </div>
              </div>

              <div className="bubble bubble--me bubble--img b1">
                <div className="bubble__photo">📄 homework.jpg</div>
                solve Q3 please
              </div>

              <div className="bubble bubble--them b2">
                <span className="typing">
                  <span />
                  <span />
                  <span />
                </span>
              </div>

              <div className="bubble bubble--them b3">
                Got it - that&apos;s the quadratic 2x² − 7x + 3 = 0.
                <br />
                <br />
                Before I solve it: what do you get if you multiply <b>a×c</b>?
              </div>

              <div className="bubble bubble--me b4">6</div>

              <div className="bubble bubble--them b5">
                Exactly 👏 Now find two numbers that multiply to 6 and add to −7...
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="section section--warm" id="features">
        <div className="shell">
          <div className="section__head">
            <h2>Everything a study buddy should do</h2>
            <p>
              Not a chatbot that answers and disappears. It reads what you send,
              works through it the way a teacher would, and remembers what you
              are struggling with.
            </p>
          </div>

          <div className="grid">
            {FEATURES.map((feature, index) => (
              <article
                key={feature.title}
                className={`card reveal d${Math.min(index + 1, 6)}`}
              >
                <div className="card__icon" aria-hidden="true">
                  {feature.icon}
                </div>
                <h3>{feature.title}</h3>
                <p>{feature.body}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="how">
        <div className="shell">
          <div className="section__head">
            <h2>Three steps, then you are done</h2>
            <p>
              You give it your teacher&apos;s name so it answers to something
              familiar. It never pretends to be them - it just sounds like a
              tutor you already trust.
            </p>
          </div>

          <div className="steps">
            {STEPS.map((step, index) => (
              <article key={step.title} className={`step reveal d${index + 2}`}>
                <h3>{step.title}</h3>
                <p>{step.body}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="section section--warm" id="pricing">
        <div className="shell">
          <div className="section__head center" style={{ marginInline: "auto" }}>
            <h2>One plan. Everything included.</h2>
            <p>No tiers, no per-question charges, no surprise upgrade screens.</p>
          </div>

          <div className="pricing">
            <div className="plan reveal d2">
              <span className="plan__tag">Launch price</span>
              <div className="plan__price">
                ₹100<span> / 30 days</span>
              </div>
              <p className="muted">Everything below, from the moment you pay.</p>

              <ul className="plan__list">
                {[
                  "Unlimited questions by text, photo or voice",
                  "Homework photos read with OCR and vision",
                  "PDF and worksheet reading",
                  "Symbolically verified maths",
                  "Graphs and diagrams sent to your chat",
                  "Practice questions, quizzes and marking",
                  "Your tutor, named by you",
                ].map((item) => (
                  <li key={item}>
                    <span className="tick" aria-hidden="true">
                      ✓
                    </span>
                    {item}
                  </li>
                ))}
              </ul>

              <a className="btn btn--lg btn--block" href="/payment">
                Get subscription
              </a>
              <p className="muted" style={{ marginTop: "0.9rem", fontSize: "0.86rem" }}>
                Activated on WhatsApp within seconds of payment.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="shell stack-lg center">
          <h2>Stop being stuck at 11pm.</h2>
          <p className="muted" style={{ maxWidth: "52ch" }}>
            Your tutor is already in the one app you never close.
          </p>
          <a className="btn btn--lg" href="/payment">
            Get subscription - ₹100
          </a>
        </div>
      </section>
    </>
  );
}
