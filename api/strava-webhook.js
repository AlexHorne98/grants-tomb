/**
 * Vercel serverless function — api/strava-webhook.js
 *
 * Handles two things:
 *   GET  ?hub.challenge=...  →  Strava subscription verification
 *   POST {object_type, aspect_type, owner_id, object_id} → triggers GitHub Actions
 */

export default async function handler(req, res) {

  // ── GET: Strava subscription verification ────────────────────────────────
  if (req.method === 'GET') {
    const challenge = req.query['hub.challenge'];
    const verifyToken = req.query['hub.verify_token'];

    if (verifyToken !== process.env.STRAVA_VERIFY_TOKEN) {
      console.error('Invalid verify token:', verifyToken);
      return res.status(403).json({ error: 'Invalid verify token' });
    }

    console.log('Strava webhook verified ✅');
    return res.status(200).json({ 'hub.challenge': challenge });
  }

  // ── POST: Activity event ──────────────────────────────────────────────────
  if (req.method === 'POST') {
    const event = req.body;
    console.log('Strava webhook event:', JSON.stringify(event));

    // Only care about new activities (not updates/deletes)
    if (event.object_type !== 'activity' || event.aspect_type !== 'create') {
      return res.status(200).json({ status: 'ignored', reason: 'not a new activity' });
    }

    const activityId = event.object_id;
    console.log(`New activity created: ${activityId} — triggering GitHub Actions`);

    // Trigger GitHub Actions repository_dispatch
    const owner = process.env.GITHUB_OWNER;
    const repo  = process.env.GITHUB_REPO;
    const token = process.env.GITHUB_PAT;

    const ghRes = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `token ${token}`,
          'Accept': 'application/vnd.github.v3+json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          event_type: 'strava_activity',
          client_payload: { activity_id: String(activityId) }
        })
      }
    );

    if (!ghRes.ok) {
      const body = await ghRes.text();
      console.error('GitHub dispatch failed:', ghRes.status, body);
      return res.status(500).json({ error: 'Failed to trigger GitHub Actions' });
    }

    console.log('GitHub Actions triggered ✅');
    return res.status(200).json({ status: 'ok', activity_id: activityId });
  }

  return res.status(405).json({ error: 'Method not allowed' });
}
