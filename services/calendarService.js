const fs = require('fs');
const { google } = require('googleapis');
const { BaseService } = require('../lib/BaseService');
const { AUTH_PATH } = require('../lib/paths');

/**
 * Calendar Service (Google Calendar) - OPTIONAL
 * Provides upcoming calendar events
 */
class CalendarService extends BaseService {
  constructor(cacheTTLMinutes = 30) {
    super({
      name: 'Calendar',
      cacheKey: 'calendar',
      cacheTTL: cacheTTLMinutes * 60 * 1000,
      retryAttempts: 2,
      retryCooldown: 1000,
    });
  }

  isEnabled() {
    const clientId = process.env.GOOGLE_CLIENT_ID;
    const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
    const auth = this.loadTokens();
    const hasTokens = auth.google?.tokens != null;
    return !!(clientId && clientSecret && hasTokens);
  }

  loadTokens() {
    try {
      if (fs.existsSync(AUTH_PATH)) {
        const auth = JSON.parse(fs.readFileSync(AUTH_PATH, 'utf-8'));
        return auth;
      }
    } catch (_) {}
    return {};
  }

  getOAuthClient(baseUrl) {
    const clientId = process.env.GOOGLE_CLIENT_ID;
    const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
    const redirectUri = process.env.GOOGLE_REDIRECT_URI || `${baseUrl}/auth/google/callback`;
    
    return new google.auth.OAuth2(clientId, clientSecret, redirectUri);
  }

  getAuthorizedClient(baseUrl) {
    const oauth2Client = this.getOAuthClient(baseUrl);
    const auth = this.loadTokens();
    const tokens = auth.google?.tokens;
    if (!tokens) return null;

    oauth2Client.setCredentials(tokens);
    oauth2Client.on('tokens', (newTokens) => {
      const merged = { ...(tokens || {}), ...newTokens };
      this.saveTokens(merged);
    });

    return oauth2Client;
  }

  saveTokens(tokens) {
    try {
      const auth = fs.existsSync(AUTH_PATH) 
        ? JSON.parse(fs.readFileSync(AUTH_PATH, 'utf-8')) 
        : {};
      auth.google = auth.google || {};
      auth.google.tokens = tokens;
      fs.writeFileSync(AUTH_PATH, JSON.stringify(auth, null, 2));
    } catch (e) {
      console.warn('Failed to save Google tokens:', e.message);
    }
  }

  async fetchData(config, logger) {
    const authClient = this.getAuthorizedClient(config.baseUrl);
    if (!authClient) throw new Error('Google not authenticated');

    // Read selected calendars from auth.json
    const authData = this.loadTokens();
    const calendarIds = authData.google?.selectedCalendars || [];
    const timezone = config.timezone || 'Asia/Kolkata';

    if (calendarIds.length === 0) {
      throw new Error('No calendars selected');
    }
    
    const calendar = google.calendar({ version: 'v3', auth: authClient });

    const now = new Date();
    const timeMin = now.toISOString();
    const timeMax = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000).toISOString();

    const events = [];
    for (const calId of calendarIds) {
      try {
        const res = await calendar.events.list({
          calendarId: calId,
          timeMin,
          timeMax,
          maxResults: 10,
          singleEvents: true,
          orderBy: 'startTime',
          timeZone: timezone,
        });

        const items = res.data.items || [];
        for (const ev of items) {
          // Skip all-day events
          if (ev.start && ev.start.date && !ev.start.dateTime) continue;
          
          const start = ev.start.dateTime || ev.start.date;
          const startDate = new Date(start);
          
          // Only include events within next 7 days
          const diffMs = startDate - now;
          const days = diffMs / (1000 * 60 * 60 * 24);
          if (days < 0 || days > 7) continue;

          events.push({
            title: ev.summary || 'Untitled',
            start,
            startDate,
          });
        }
      } catch (e) {
        logger.warn?.(`[Calendar] Failed to fetch events for ${calId}: ${e.message}`);
      }
    }

    events.sort((a, b) => a.startDate - b.startDate);
    return { events: events.slice(0, 2), timezone };
  }

  mapToDashboard(apiData, config) {
    const now = new Date();
    const timezone = apiData.timezone || 'Asia/Kolkata';

    return apiData.events.map(ev => ({
      title: ev.title,
      time: this.formatRelativeTime(ev.start, now, timezone),
    }));
  }

  formatRelativeTime(dateStr, now, tz) {
    const target = new Date(dateStr);
    const sameDay = target.toLocaleDateString('en-US', { timeZone: tz }) === now.toLocaleDateString('en-US', { timeZone: tz });
    
    const tomorrow = new Date(now);
    tomorrow.setDate(now.getDate() + 1);
    const isTomorrow = target.toLocaleDateString('en-US', { timeZone: tz }) === tomorrow.toLocaleDateString('en-US', { timeZone: tz });
    
    const timeStr = target.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: tz }).toLowerCase();
    
    if (sameDay) return `Today at ${timeStr}`;
    if (isTomorrow) return `Tomorrow at ${timeStr}`;
    
    const diffDays = Math.round((target - now) / (1000 * 60 * 60 * 24));
    if (diffDays > 0 && diffDays <= 7) return `In ${diffDays} days at ${timeStr}`;
    
    const weekday = target.toLocaleDateString('en-US', { weekday: 'long', timeZone: tz });
    return `${weekday} at ${timeStr}`;
  }
}

// OAuth helper functions for server.js
const service = new CalendarService();

function buildAuthUrl(baseUrl) {
  const oauth2Client = service.getOAuthClient(baseUrl);
  const scopes = ['https://www.googleapis.com/auth/calendar.readonly'];
  return oauth2Client.generateAuthUrl({
    access_type: 'offline',
    prompt: 'consent',
    scope: scopes,
  });
}

async function handleOAuthCallback(baseUrl, code) {
  const oauth2Client = service.getOAuthClient(baseUrl);
  const { tokens } = await oauth2Client.getToken(code);
  service.saveTokens(tokens);
  return tokens;
}

function isAuthed() {
  const auth = service.loadTokens();
  const tokens = auth.google?.tokens;
  return !!(tokens && (tokens.refresh_token || tokens.access_token));
}

async function listCalendars(baseUrl, logger = console) {
  const auth = service.getAuthorizedClient(baseUrl);
  if (!auth) throw new Error('Google not authenticated');
  const calendar = google.calendar({ version: 'v3', auth });
  const res = await calendar.calendarList.list({ maxResults: 250 });
  const items = res.data.items || [];
  return items.map(it => ({ id: it.id, summary: it.summary, primary: !!it.primary }));
}

module.exports = {
  CalendarService,
  buildAuthUrl,
  handleOAuthCallback,
  isAuthed,
  listCalendars,
};
