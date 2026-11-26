ALTER TABLE feedback_surveys ADD COLUMN rating_stars INTEGER
 CHECK(rating_stars IS NULL OR (typeof(rating_stars)='integer' AND rating_stars BETWEEN 1 AND 5));
ALTER TABLE feedback_surveys ADD COLUMN rating_scale TEXT NOT NULL DEFAULT 'binary'
 CHECK((rating_scale='binary' AND rating_stars IS NULL) OR (rating_scale='stars' AND rating IS NULL));

-- Only invitations that have never been sent or attempted can change scale.
-- Preserve old buttons, tokens and ratings, including uncertain deliveries.
UPDATE feedback_surveys SET rating_scale='stars', text='Как вы оцениваете помощь бота?

Выберите от 1 до 5 звезд: 1 — совсем не помог, 5 — полностью помог.'
 WHERE status IN ('pending','sending') AND sent_at IS NULL AND attempted_at IS NULL
 AND attempts=0 AND rating IS NULL;
