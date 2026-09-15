import { createFileRoute } from "@tanstack/react-router";
import { LegalList, LegalPage, LegalSection, SupportLink } from "@/components/legal-page";

const TITLE = "Privacy Policy · ResearchMind Lab";
const SUMMARY =
  "What ResearchMind Lab collects when you sign in with Google and upload papers, where it is stored, who processes it, and how to have it deleted.";

export const Route = createFileRoute("/privacy")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: SUMMARY },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: SUMMARY },
    ],
  }),
  component: PrivacyPage,
});

function PrivacyPage() {
  return (
    <LegalPage title="Privacy Policy" updated="16 September 2026">
      <LegalSection title="Who is responsible">
        <p>
          ResearchMind Lab is operated by Harish Kumar S G, an individual based in India, who is the
          data controller for the information described here. In this policy, &ldquo;I&rdquo; means
          that person and &ldquo;you&rdquo; means the person using the service.
        </p>
        <p>
          For anything in this policy, including requests about your data, email <SupportLink />.
        </p>
      </LegalSection>

      <LegalSection title="What signing in with Google gives me">
        <p>
          Signing in uses Google, through Supabase Auth. The sign-in requests two standard scopes,{" "}
          <span className="font-mono text-[13px]">email</span> and{" "}
          <span className="font-mono text-[13px]">profile</span>, which give me your email address
          and basic profile information such as your name and profile picture.
        </p>
        <p>
          No other Google scopes are requested. ResearchMind Lab has no access to your Gmail, Drive,
          Calendar, Contacts or any other Google service, and cannot read or change anything in your
          Google account beyond that basic identity.
        </p>
        <p>
          A session token is stored in your browser so you stay signed in between visits. Clearing
          your browser storage signs you out.
        </p>
      </LegalSection>

      <LegalSection title="What you give me by using the service">
        <LegalList
          items={[
            "The PDF papers you upload, up to 25 MB each. Only PDF files are accepted.",
            "Text extracted from those PDFs, split into passages, together with the title, page numbers, and the author, abstract and keyword lines detected in the document.",
            "The questions you ask, the search queries you type, and the report topics you enter.",
            "The answers, comparisons and reports generated for you, including their citations.",
            "Your chat history with the assistant, message by message.",
          ]}
        />
      </LegalSection>

      <LegalSection title="Where it is stored">
        <LegalList
          items={[
            <>
              <strong className="font-medium">Supabase Auth</strong> holds your account record and
              the identity from Google.
            </>,
            <>
              <strong className="font-medium">Supabase Storage</strong> holds your original PDF
              files, each under a folder belonging to your account only.
            </>,
            <>
              <strong className="font-medium">Supabase Postgres</strong> holds the record of each
              paper (title, file size, a fingerprint of the file contents, its storage location,
              status and timestamps), your generated reports, and your chat messages. Database rows
              are restricted per account using row-level security.
            </>,
            <>
              <strong className="font-medium">Qdrant</strong> holds the search index: the numeric
              embedding of each passage together with the passage text itself and its paper and page
              labels. Every entry carries the account it belongs to, and searches are filtered to
              that account.
            </>,
          ]}
        />
      </LegalSection>

      <LegalSection title="Third parties that process your content">
        <p>
          Two AI providers receive parts of your content, because the features cannot work without
          them:
        </p>
        <LegalList
          items={[
            <>
              <strong className="font-medium">Voyage AI</strong> turns text into embeddings.
              Passages from your papers are sent when a paper is indexed. Shorter text is sent
              whenever a feature has to match something against your library: a search, a question,
              a paper comparison, a report topic, or opening a paper&rsquo;s details or summary.
            </>,
            <>
              <strong className="font-medium">Groq</strong> generates answers, comparisons and
              reports. Your question and the passages retrieved from your own papers are sent to it,
              along with the instructions that tell it to answer only from those passages.
            </>,
          ]}
        />
        <p>
          These providers process that content under their own terms and privacy policies. What they
          retain, and whether they use submitted content for their own purposes, is governed by
          those terms rather than by me, so please read them if that matters to you.
        </p>
        <p>
          The website is served by Vercel and the API runs on Railway. As the infrastructure
          handling your requests, those providers may receive standard request information such as
          your IP address and browser information. The application itself does not read or store
          either.
        </p>
        <p>
          Every page, including the signed-in application, loads typefaces from Google Fonts, so
          Google receives your IP address and browser information whenever a page loads.
        </p>
        <p>
          Diagnostic logs on the API host can contain the text you submit &mdash; a search query or
          a report topic, for example &mdash; as part of ordinary application logging.
        </p>
      </LegalSection>

      <LegalSection title="What I do not do">
        <LegalList
          items={[
            "I do not sell your data, and I do not share it for advertising.",
            "I do not use your papers, questions, chats or reports to train any model of my own.",
            "There is no analytics, advertising or tracking script on this site, and no tracking cookies are set.",
          ]}
        />
      </LegalSection>

      <LegalSection title="How long it is kept, and what deletion removes">
        <p>
          There is no automatic expiry. What you upload and generate is kept until you delete it or
          ask me to delete your account.
        </p>
        <p>
          Deleting a paper from its page in the app removes three things: its passages and
          embeddings from Qdrant, the original PDF from storage, and its record from the database.
        </p>
        <p>
          Deleting a paper does <strong className="font-medium">not</strong> remove reports you have
          already generated, which may quote or cite that paper, and does not remove your chat
          history. Those are kept until your account is deleted. I would rather state this plainly
          than let you assume a paper deletion erases everything derived from it.
        </p>
        <p>
          To remove everything, email <SupportLink /> and ask for account deletion. There is no
          self-service button for this: full deletion is carried out manually, by me. It removes
          your account record, your uploaded PDF files, the indexed passages and embeddings derived
          from them, your reports and your chat history. I will confirm by email once it is done.
        </p>
      </LegalSection>

      <LegalSection title="Why your content is processed">
        <p>
          Your papers and questions are processed for one purpose: to provide the features you have
          asked for — storing what you upload, indexing it, searching it and answering from it — and
          to keep the service secure and working.
        </p>
        <p>
          Where a privacy law such as the UK or EU GDPR applies to that processing, those purposes
          are what I rely on: performing the service you requested, and a legitimate interest in
          keeping it secure and free from abuse. Which law applies to you depends on your
          circumstances, and nothing here is a legal determination about that.
        </p>
      </LegalSection>

      <LegalSection title="Your rights">
        <p>
          If an applicable privacy law gives you rights over your personal data, those rights may
          include asking for a copy of it, correcting it, having it erased, restricting or objecting
          to its processing, and receiving it in a portable form. Which of these you have, if any,
          depends on the law that applies to you.
        </p>
        <p>
          Whatever your legal position, you can email <SupportLink /> to ask for a copy of your data
          or to have it deleted, and I will act on it. I aim to respond promptly, and within any
          deadline that an applicable law sets for such a request.
        </p>
        <p>
          Depending on where you live, you may also be able to complain to a data protection
          authority if you think your data has been handled improperly.
        </p>
      </LegalSection>

      <LegalSection title="Where processing happens">
        <p>
          The storage, hosting and AI providers named above operate their own infrastructure in
          various countries. Your data may therefore be stored or processed outside the country you
          are in, including in countries whose data protection rules differ from your own, under
          each provider&rsquo;s terms.
        </p>
      </LegalSection>

      <LegalSection title="Security">
        <p>
          The site and API are served over HTTPS. Every request for your data is checked against
          your signed-in account: database rows are restricted per account with row-level security,
          stored files sit under per-account paths with their own access rules, and search results
          are filtered to your own papers before anything is returned.
        </p>
        <p>
          No service can promise perfect security, and I make no such promise here. If you believe
          your account or data has been exposed, email <SupportLink />.
        </p>
      </LegalSection>

      <LegalSection title="Children">
        <p>
          ResearchMind Lab is not intended for anyone under 16, and accounts should not be created
          by them. If you believe someone under 16 has an account, email <SupportLink /> and I will
          remove it.
        </p>
      </LegalSection>

      <LegalSection title="Changes to this policy">
        <p>
          If this policy changes, the revised version appears on this page with a new date at the
          top. Significant changes to how your content is handled will be noted here rather than
          made quietly.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
