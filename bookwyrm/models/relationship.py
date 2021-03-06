''' defines relationships between users '''
from django.apps import apps
from django.db import models, transaction
from django.db.models import Q
from django.dispatch import receiver

from bookwyrm import activitypub
from .activitypub_mixin import ActivitypubMixin, ActivityMixin
from .base_model import BookWyrmModel
from . import fields


class UserRelationship(BookWyrmModel):
    ''' many-to-many through table for followers '''
    user_subject = fields.ForeignKey(
        'User',
        on_delete=models.PROTECT,
        related_name='%(class)s_user_subject',
        activitypub_field='actor',
    )
    user_object = fields.ForeignKey(
        'User',
        on_delete=models.PROTECT,
        related_name='%(class)s_user_object',
        activitypub_field='object',
    )

    @property
    def privacy(self):
        ''' all relationships are handled directly with the participants '''
        return 'direct'

    @property
    def recipients(self):
        ''' the remote user needs to recieve direct broadcasts '''
        return [u for u in [self.user_subject, self.user_object] if not u.local]

    class Meta:
        ''' relationships should be unique '''
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=['user_subject', 'user_object'],
                name='%(class)s_unique'
            ),
            models.CheckConstraint(
                check=~models.Q(user_subject=models.F('user_object')),
                name='%(class)s_no_self'
            )
        ]

    def get_remote_id(self, status=None):# pylint: disable=arguments-differ
        ''' use shelf identifier in remote_id '''
        status = status or 'follows'
        base_path = self.user_subject.remote_id
        return '%s#%s/%d' % (base_path, status, self.id)


class UserFollows(ActivitypubMixin, UserRelationship):
    ''' Following a user '''
    status = 'follows'
    activity_serializer = activitypub.Follow


    @classmethod
    def from_request(cls, follow_request):
        ''' converts a follow request into a follow relationship '''
        return cls.objects.create(
            user_subject=follow_request.user_subject,
            user_object=follow_request.user_object,
            remote_id=follow_request.remote_id,
        )


class UserFollowRequest(ActivitypubMixin, UserRelationship):
    ''' following a user requires manual or automatic confirmation '''
    status = 'follow_request'
    activity_serializer = activitypub.Follow

    def save(self, *args, broadcast=True, **kwargs):
        ''' make sure the follow or block relationship doesn't already exist '''
        try:
            UserFollows.objects.get(
                user_subject=self.user_subject,
                user_object=self.user_object,
            )
            # blocking in either direction is a no-go
            UserBlocks.objects.get(
                user_subject=self.user_subject,
                user_object=self.user_object,
            )
            UserBlocks.objects.get(
                user_subject=self.user_object,
                user_object=self.user_subject,
            )
            return None
        except (UserFollows.DoesNotExist, UserBlocks.DoesNotExist):
            super().save(*args, **kwargs)

        if broadcast and self.user_subject.local and not self.user_object.local:
            self.broadcast(self.to_activity(), self.user_subject)

        if self.user_object.local:
            model = apps.get_model('bookwyrm.Notification', require_ready=True)
            notification_type = 'FOLLOW_REQUEST' \
                if self.user_object.manually_approves_followers else 'FOLLOW'
            model.objects.create(
                user=self.user_object,
                related_user=self.user_subject,
                notification_type=notification_type,
            )


    def accept(self):
        ''' turn this request into the real deal'''
        user = self.user_object
        activity = activitypub.Accept(
            id=self.get_remote_id(status='accepts'),
            actor=self.user_object.remote_id,
            object=self.to_activity()
        ).serialize()
        with transaction.atomic():
            UserFollows.from_request(self)
            self.delete()

        self.broadcast(activity, user)


    def reject(self):
        ''' generate a Reject for this follow request '''
        user = self.user_object
        activity = activitypub.Reject(
            id=self.get_remote_id(status='rejects'),
            actor=self.user_object.remote_id,
            object=self.to_activity()
        ).serialize()
        self.delete()
        self.broadcast(activity, user)


class UserBlocks(ActivityMixin, UserRelationship):
    ''' prevent another user from following you and seeing your posts '''
    status = 'blocks'
    activity_serializer = activitypub.Block


@receiver(models.signals.post_save, sender=UserBlocks)
#pylint: disable=unused-argument
def execute_after_save(sender, instance, created, *args, **kwargs):
    ''' remove follow or follow request rels after a block is created '''
    UserFollows.objects.filter(
        Q(user_subject=instance.user_subject,
          user_object=instance.user_object) | \
        Q(user_subject=instance.user_object,
          user_object=instance.user_subject)
    ).delete()
    UserFollowRequest.objects.filter(
        Q(user_subject=instance.user_subject,
          user_object=instance.user_object) | \
        Q(user_subject=instance.user_object,
          user_object=instance.user_subject)
    ).delete()
