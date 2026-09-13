-- Phase 1.5 Storage setup. NOT executed yet.
--
-- STEP 1 (manual — dashboard or Management API, not SQL): create a
-- PRIVATE bucket named 'papers' in Supabase Storage. Do not mark it
-- public — there is no current product feature that requires it, and
-- research papers are private-per-user by design.
--
-- STEP 2 (this file): RLS policies on storage.objects enforcing that a
-- user may only insert/select/delete objects under their own owner_id
-- folder — i.e. paths shaped exactly like:
--   papers/{owner_id}/{paper_id}/original.pdf
--
-- These policies are the REAL, active enforcement layer only for calls
-- made using the calling user's own JWT. backend/app/services/storage.py
-- deliberately uses the user's verified JWT for upload/delete (so these
-- policies actually apply), and only uses the service-role key (which
-- bypasses RLS entirely) inside the indexing job, which has no live user
-- request to scope a JWT to.

create policy "Users can upload to their own folder"
on storage.objects for insert
to authenticated
with check (
    bucket_id = 'papers'
    and (storage.foldername(name))[1] = auth.uid()::text
);

create policy "Users can read their own files"
on storage.objects for select
to authenticated
using (
    bucket_id = 'papers'
    and (storage.foldername(name))[1] = auth.uid()::text
);

create policy "Users can delete their own files"
on storage.objects for delete
to authenticated
using (
    bucket_id = 'papers'
    and (storage.foldername(name))[1] = auth.uid()::text
);
