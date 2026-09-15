import { createFileRoute } from "@tanstack/react-router";
import { LegalList, LegalPage, LegalSection, SupportLink } from "@/components/legal-page";

const TITLE = "Contact · ResearchMind Lab";
const SUMMARY = "How to reach ResearchMind Lab for support, privacy requests and account deletion.";

export const Route = createFileRoute("/contact")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: SUMMARY },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: SUMMARY },
    ],
  }),
  component: ContactPage,
});

function ContactPage() {
  return (
    <LegalPage title="Contact" updated="16 September 2026">
      <LegalSection title="Email">
        <p>
          ResearchMind Lab is built and run by one person, Harish Kumar S G, in India. Email is the
          only support channel — there is no phone line and no live chat.
        </p>
        <p>
          Write to <SupportLink />.
        </p>
      </LegalSection>

      <LegalSection title="What to write about">
        <LegalList
          items={[
            "Questions or problems with uploading, indexing, search, Ask AI, comparisons or reports.",
            "Privacy and data protection requests — a copy of your data, a correction, or erasure.",
            "Deleting your account and everything stored with it.",
            "Copyright concerns about material held in the service.",
          ]}
        />
      </LegalSection>

      <LegalSection title="Response times">
        <p>
          I aim to reply to support email within a few working days. Requests about your data are
          handled as promptly as I can manage, and within any deadline that an applicable privacy
          law sets for them; if a request will take longer than expected, I will tell you.
        </p>
      </LegalSection>

      <LegalSection title="Deleting your data">
        <p>
          You can delete any individual paper yourself from its page in the app, which removes the
          PDF, its indexed passages and its database record. Reports and chat history you have
          already generated are kept — see the Privacy Policy for exactly what a paper deletion does
          and does not remove.
        </p>
        <p>
          To delete your whole account, email <SupportLink /> from the address you signed in with,
          or tell me which account to remove. There is no self-service account deletion: I carry it
          out manually, removing your account record, uploaded files, indexed passages, reports and
          chat history, and I confirm by email once it is done.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
