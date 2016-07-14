import ddt
from django.conf import settings
import mock
from oscar.core.loading import get_model
from oscar.test.factories import create_order
from oscar.test.newfactories import BasketFactory

from ecommerce.core.constants import ENROLLMENT_CODE_PRODUCT_CLASS_NAME, ENROLLMENT_CODE_SWITCH
from ecommerce.core.tests import toggle_switch
from ecommerce.courses.models import Course
from ecommerce.courses.publishers import LMSPublisher
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.extensions.catalogue.tests.mixins import CourseCatalogTestMixin
from ecommerce.tests.testcases import TestCase

Product = get_model('catalogue', 'Product')
ProductClass = get_model('catalogue', 'ProductClass')
StockRecord = get_model('partner', 'StockRecord')


@ddt.ddt
class CourseTests(CourseCatalogTestMixin, TestCase):
    def test_unicode(self):
        """Verify the __unicode__ method returns the Course ID."""
        course_id = u'edx/Demo_Course/DemoX'
        course = Course.objects.create(id=course_id)
        self.assertEqual(unicode(course), course_id)

    def test_seat_products(self):
        """
        Verify the method returns a list containing purchasable course seats.

        These seats should be the child products.
        """
        # Create a new course and verify it has a parent product, but no children.
        course = CourseFactory()
        self.assertEqual(course.products.count(), 1)
        self.assertEqual(len(course.seat_products), 0)

        # Create the seat products
        toggle_switch(ENROLLMENT_CODE_SWITCH, True)
        seats = [course.create_or_update_seat('honor', False, 0, self.partner),
                 course.create_or_update_seat('verified', True, 50, self.partner, create_enrollment_code=True)]
        self.assertEqual(course.products.count(), 4)

        # The property should return only the child seats.
        self.assertEqual(set(course.seat_products), set(seats))

    @ddt.data(
        ('verified', True),
        ('credit', True),
        ('professional', True),
        ('honor', False),
        ('no-id-professional', False),
        ('audit', False),
        ('unknown', False),
    )
    @ddt.unpack
    def test_is_mode_verified(self, mode, expected):
        """ Verify the method returns True only for verified modes. """
        self.assertEqual(Course.is_mode_verified(mode), expected)

    @ddt.data(
        ('Verified', 'verified'),
        ('credit', 'credit'),
        ('professional', 'professional'),
        ('honor', 'honor'),
        ('no-id-professional', 'professional'),
        ('audit', ''),
        ('unknown', 'unknown'),
    )
    @ddt.unpack
    def test_certificate_type_for_mode(self, mode, expected):
        """ Verify the method returns the correct certificate type for a given mode. """
        self.assertEqual(Course.certificate_type_for_mode(mode), expected)

    def test_publish_to_lms(self):
        """ Verify the method publishes data to LMS. """
        course = CourseFactory()
        with mock.patch.object(LMSPublisher, 'publish') as mock_publish:
            course.publish_to_lms()
            self.assertTrue(mock_publish.called)

    def test_save_creates_parent_seat(self):
        """ Verify the save method creates a parent seat if one does not exist. """
        course = Course.objects.create(id='a/b/c', name='Test Course')
        self.assertEqual(course.products.count(), 1)

        parent = course.parent_seat_product
        self.assertEqual(parent.structure, Product.PARENT)
        self.assertEqual(parent.title, 'Seat in Test Course')
        self.assertEqual(parent.get_product_class(), self.seat_product_class)
        self.assertEqual(parent.attr.course_key, course.id)

    def assert_course_seat_valid(self, seat, course, certificate_type, id_verification_required, price,
                                 credit_provider=None, credit_hours=None):
        """ Ensure the given seat has the correct attribute values. """
        self.assertEqual(seat.structure, Product.CHILD)
        # pylint: disable=protected-access
        self.assertEqual(seat.title, course.get_course_seat_name(certificate_type, id_verification_required))
        self.assertEqual(seat.get_product_class(), self.seat_product_class)
        self.assertEqual(getattr(seat.attr, 'certificate_type', ''), certificate_type)
        self.assertEqual(seat.attr.course_key, course.id)
        self.assertEqual(seat.attr.id_verification_required, id_verification_required)
        self.assertEqual(seat.stockrecords.first().price_excl_tax, price)

        if credit_provider:
            self.assertEqual(seat.attr.credit_provider, credit_provider)

        if credit_hours:
            self.assertEqual(seat.attr.credit_hours, credit_hours)

    def test_create_or_update_seat(self):
        """ Verify the method creates or updates a seat Product. """
        course = Course.objects.create(id='a/b/c', name='Test Course')

        # Test seat creation
        certificate_type = 'verified'
        id_verification_required = True
        price = 5
        course.create_or_update_seat(certificate_type, id_verification_required, price, self.partner)

        # Two seats: one verified, the other the parent seat product
        self.assertEqual(course.products.count(), 2)
        seat = course.seat_products[0]
        self.assert_course_seat_valid(seat, course, certificate_type, id_verification_required, price)

        # Test seat update
        price = 10
        course.create_or_update_seat(certificate_type, id_verification_required, price, self.partner)

        # Again, only two seats with one being the parent seat product.
        self.assertEqual(course.products.count(), 2)
        seat = course.seat_products[0]
        self.assert_course_seat_valid(seat, course, certificate_type, id_verification_required, price)

    def test_create_seat_with_enrollment_code(self):
        """Verify an enrollment code product is created."""
        course = CourseFactory()
        seat_type = 'verified'
        price = 5
        toggle_switch(ENROLLMENT_CODE_SWITCH, True)
        course.create_or_update_seat(seat_type, True, price, self.partner, create_enrollment_code=True)

        enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
        self.assertEqual(enrollment_code.attr.course_key, course.id)
        self.assertEqual(enrollment_code.attr.seat_type, seat_type)

        # Second time should skip over the enrollment code creation logic but result in the same data
        course.create_or_update_seat(seat_type, True, price, self.partner)
        enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
        self.assertEqual(enrollment_code.attr.course_key, course.id)
        self.assertEqual(enrollment_code.attr.seat_type, seat_type)

        stock_record = StockRecord.objects.get(product=enrollment_code)
        self.assertEqual(stock_record.price_excl_tax, price)
        self.assertEqual(stock_record.price_currency, settings.OSCAR_DEFAULT_CURRENCY)
        self.assertEqual(stock_record.partner, self.partner)

    def test_create_credit_seats(self):
        """Verify that the model's seat creation method allows the creation of multiple credit seats."""
        course = Course.objects.create(id='a/b/c', name='Test Course')
        credit_data = {'MIT': 2, 'Harvard': 1}
        certificate_type = 'credit'
        id_verification_required = True
        price = 10

        # Verify that the course can have multiple credit seats added to it
        for credit_provider, credit_hours in credit_data.iteritems():
            credit_seat = course.create_or_update_seat(
                certificate_type,
                id_verification_required,
                price,
                self.partner,
                credit_provider=credit_provider,
                credit_hours=credit_hours
            )

            self.assert_course_seat_valid(
                credit_seat,
                course,
                certificate_type,
                id_verification_required,
                price,
                credit_provider=credit_provider,
                credit_hours=credit_hours
            )

        # Expected seat total, with one being the parent seat product.
        self.assertEqual(course.products.count(), len(credit_data) + 1)

    def test_collision_avoidance(self):
        """
        Sanity check verifying that course IDs which produced collisions due to a
        lossy slug generation process no longer do so.
        """
        dotted_course = Course.objects.create(id='a/...course.../id')
        regular_course = Course.objects.create(id='a/course/id')

        certificate_type = 'honor'
        id_verification_required = False
        price = 0
        dotted_course.create_or_update_seat(certificate_type, id_verification_required, price, self.partner)
        regular_course.create_or_update_seat(certificate_type, id_verification_required, price, self.partner)

        child_products = Product.objects.filter(structure=Product.CHILD).count()
        self.assertEqual(child_products, 2)

    def test_prof_ed_stale_product_removal(self):
        """
        Verify that stale professional education seats are deleted if they have not been purchased.
        """
        course = CourseFactory()
        course.create_or_update_seat('professional', False, 0, self.partner)
        self.assertEqual(course.products.count(), 2)

        course.create_or_update_seat('professional', True, 0, self.partner)
        self.assertEqual(course.products.count(), 2)

        product_mode = course.products.first()
        self.assertEqual(product_mode.attr.id_verification_required, True)
        self.assertEqual(product_mode.attr.certificate_type, 'professional')

    def test_prof_ed_stale_product_removal_with_orders(self):
        """
        Verify that professional education seats are never deleted if they have been purchased.
        """
        user = self.create_user()
        course = CourseFactory()
        professional_product_no_verification = course.create_or_update_seat('professional', False, 0, self.partner)
        self.assertEqual(course.products.count(), 2)

        basket = BasketFactory(owner=user)
        basket.add_product(professional_product_no_verification)
        create_order(basket=basket, user=user)
        course.create_or_update_seat('professional', True, 0, self.partner)
        self.assertEqual(course.products.count(), 3)

        product_mode = course.products.all()[0]
        self.assertEqual(product_mode.attr.id_verification_required, True)
        self.assertEqual(product_mode.attr.certificate_type, 'professional')

        product_mode = course.products.all()[1]
        self.assertEqual(product_mode.attr.id_verification_required, False)
        self.assertEqual(product_mode.attr.certificate_type, 'professional')

    def test_create_or_update_seat_without_stale_seat_removal(self):
        """
        Verify that professional education seats are not deleted if remove_stale_modes flag is not set.
        """
        course = CourseFactory()
        course.create_or_update_seat('professional', False, 0, self.partner)
        self.assertEqual(course.products.count(), 2)

        course.create_or_update_seat('professional', True, 0, self.partner, remove_stale_modes=False)
        self.assertEqual(course.products.count(), 3)

        product_mode = course.products.all()[0]
        self.assertEqual(product_mode.attr.id_verification_required, True)
        self.assertEqual(product_mode.attr.certificate_type, 'professional')

        product_mode = course.products.all()[1]
        self.assertEqual(product_mode.attr.id_verification_required, False)
        self.assertEqual(product_mode.attr.certificate_type, 'professional')

    def test_type(self):
        """ Verify the property returns a type value corresponding to the available products. """
        course = Course.objects.create(id='a/b/c', name='Test Course')
        self.assertEqual(course.type, 'audit')

        course.create_or_update_seat('audit', False, 0, self.partner)
        self.assertEqual(course.type, 'audit')

        course.create_or_update_seat('verified', True, 10, self.partner)
        self.assertEqual(course.type, 'verified')

        seat = course.create_or_update_seat('professional', True, 100, self.partner)
        self.assertEqual(course.type, 'professional')

        seat.delete()
        self.assertEqual(course.type, 'verified')
        course.create_or_update_seat('no-id-professional', False, 100, self.partner)
        self.assertEqual(course.type, 'professional')

        course.create_or_update_seat('credit', True, 1000, self.partner, credit_provider='SMU')
        self.assertEqual(course.type, 'credit')

    def test_enrollment_code_seat_type_filter(self):
        """ Verify that the ENROLLMENT_CODE_SEAT_TYPES constant is properly applied during seat creation """
        toggle_switch(ENROLLMENT_CODE_SWITCH, True)
        course = Course.objects.create(id='test/course/123', name='Test Course 123')

        # Audit seat products should not have a corresponding enrollment code
        course.create_or_update_seat('audit', False, 0, self.partner)
        with self.assertRaises(Product.DoesNotExist):
            enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)

        # Honor seat products should not have a corresponding enrollment code
        course.create_or_update_seat('honor', False, 0, self.partner)
        with self.assertRaises(Product.DoesNotExist):
            enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)

        # Verified seat products should have a corresponding enrollment code
        course.create_or_update_seat('verified', True, 10, self.partner, create_enrollment_code=True)
        enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
        self.assertEqual(enrollment_code.attr.course_key, course.id)
        self.assertEqual(enrollment_code.attr.seat_type, 'verified')

        # One parent product, three seat products, one enrollment code product (verified) -> five total products
        self.assertEqual(course.products.count(), 5)
        self.assertEqual(len(course.seat_products), 3)  # Definitely three seat products...
